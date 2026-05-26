from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ninja.testing import TestAsyncClient

from accounts.models import APIKey, User
from chat.models import ChatThread
from daiv.api import api


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
async def authed():
    """Return (APIKey, raw_key, user) for authenticated tests."""
    user = await User.objects.acreate_user(
        username="chatuser",
        email="chat@example.com",
        password="testpass123",  # noqa: S106
    )
    key_obj, raw = await APIKey.objects.create_key(user=user, name="Test")
    return key_obj, raw, user


def _auth_headers(raw_key: str, **extra) -> dict:
    return {"Authorization": f"Bearer {raw_key}", **extra}


def _run_agent_input(**overrides) -> dict:
    return {
        "threadId": "t-1",
        "runId": "r-1",
        "state": {},
        "messages": [{"id": "m-1", "role": "user", "content": "hello"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        **overrides,
    }


def _mock_stream(*_args, **_kwargs):
    """Factory that returns an async context manager yielding a MagicMock. Used to patch
    open_checkpointer() and set_runtime_ctx() during tests so we exercise the ownership
    path without hitting Redis or cloning a repo.
    """
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def patched_streamer():
    """Patch the streaming stack so the view runs to completion without touching Redis
    or cloning a repo, and expose ``ChatRunStreamer`` so tests can assert the kwargs the
    view constructed it with. ``events()`` returns an exhausted async generator.
    """

    async def _empty_stream(_input):
        if False:
            yield

    async def _empty_events():
        return
        yield  # pragma: no cover

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_stream),
        patch("chat.api.streaming.set_runtime_ctx", _mock_stream),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
        patch("chat.api.views.ChatRunStreamer") as m_streamer_cls,
    ):
        m_agent_cls.return_value = MagicMock(run=_empty_stream)
        m_streamer_cls.return_value = MagicMock(events=_empty_events)
        yield m_streamer_cls


@pytest.mark.django_db
async def test_missing_repo_id_header_returns_404(client: TestAsyncClient, authed):
    _, raw, user = authed
    response = await client.post(
        "/chat/completions", json=_run_agent_input(), headers=_auth_headers(raw, **{"X-Ref": "main"})
    )
    assert response.status_code == 404
    await user.adelete()


@pytest.mark.django_db
async def test_missing_ref_header_returns_404(client: TestAsyncClient, authed):
    _, raw, user = authed
    response = await client.post(
        "/chat/completions", json=_run_agent_input(), headers=_auth_headers(raw, **{"X-Repo-ID": "owner/repo"})
    )
    assert response.status_code == 404
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_cross_user_thread_id_is_rejected(client: TestAsyncClient, authed):
    _, raw, user = authed
    other = await User.objects.acreate_user(
        username="owner",
        email="owner@example.com",
        password="x",  # noqa: S106
    )
    await ChatThread.objects.acreate(thread_id="t-owned", user=other, repo_id="a/b", ref="main")

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-owned"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 403
    await user.adelete()
    await other.adelete()


@pytest.mark.django_db(transaction=True)
async def test_unknown_thread_id_implicit_creates_thread(client: TestAsyncClient, authed):
    _, raw, user = authed
    with (
        patch("chat.api.streaming.open_checkpointer", _mock_stream),
        patch("chat.api.streaming.set_runtime_ctx", _mock_stream),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
    ):
        m_instance = MagicMock()

        async def _empty_stream(_input):
            if False:  # generator that yields nothing
                yield

        m_instance.run = _empty_stream
        m_agent_cls.return_value = m_instance

        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-new"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
        )

    assert response.status_code == 200
    created = await ChatThread.objects.filter(thread_id="t-new").afirst()
    assert created is not None
    assert created.user_id == user.id
    assert created.repo_id == "a/b"
    assert created.ref == "main"
    # The finally block in ChatRunStreamer.events() clears the run slot after the stream completes.
    assert created.active_run_id is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_first_message_auto_resolves_user_env_onto_thread(client: TestAsyncClient, authed):
    """When the first chat message omits the env header (Auto), the view resolves the env
    for the calling user + repo and stamps it on the freshly-created thread."""
    from sandbox_envs.models import SandboxEnvironment, Scope

    _, raw, user = authed
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
    )
    user_env = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="mine", base_image="python:3.14", repo_ids=["a/b"]
    )

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_stream),
        patch("chat.api.streaming.set_runtime_ctx", _mock_stream),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
    ):
        m_instance = MagicMock()

        async def _empty_stream(_input):
            if False:
                yield

        m_instance.run = _empty_stream
        m_agent_cls.return_value = m_instance

        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-auto"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
        )
    assert response.status_code == 200
    created = await ChatThread.objects.aget(thread_id="t-auto")
    assert created.sandbox_environment_id == user_env.id
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_keeps_original_env_even_when_resolution_would_pick_another(
    client: TestAsyncClient, authed
):
    """``get_or_create_for_user`` only applies ``sandbox_environment`` on create. A second
    request whose Auto-resolution would pick a different env must NOT overwrite the thread."""
    from sandbox_envs.models import SandboxEnvironment, Scope

    _, raw, user = authed
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    original = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Original", base_image="python:3.14", is_default=True
    )
    # Pre-create the thread with the original env, simulating a prior first-message run.
    await ChatThread.objects.acreate(
        thread_id="t-keep", user=user, repo_id="a/b", ref="main", sandbox_environment=original
    )
    # Now add a USER env that would win at Auto resolution; the existing thread must
    # ignore it because get_or_create_for_user only applies on create.
    await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="newer", base_image="python:3.14", repo_ids=["a/b"]
    )

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_stream),
        patch("chat.api.streaming.set_runtime_ctx", _mock_stream),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
    ):
        m_instance = MagicMock()

        async def _empty_stream(_input):
            if False:
                yield

        m_instance.run = _empty_stream
        m_agent_cls.return_value = m_instance

        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-keep"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
        )
    assert response.status_code == 200
    thread = await ChatThread.objects.aget(thread_id="t-keep")
    assert thread.sandbox_environment_id == original.id
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_auto_resolved_env_passed_to_streamer_on_first_turn(client: TestAsyncClient, authed, patched_streamer):
    """First-turn Auto with a successful resolution → ``auto_resolved_env`` carries the
    {id, name, scope} of the resolved env so the streamer can swap the locked pill."""
    from sandbox_envs.models import SandboxEnvironment, Scope

    _, raw, user = authed
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user_env = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="mine", base_image="python:3.14", repo_ids=["a/b"]
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-auto-emit"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 200
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] == {
        "id": str(user_env.id),
        "name": "mine",
        "scope": "user",
    }
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_auto_submit_does_not_emit_resolved_env(
    client: TestAsyncClient, authed, patched_streamer
):
    """Existing-thread Auto submit must NOT emit ``auto_resolved_env``: the freshly
    resolved env is discarded in favour of the thread's stored env, and emitting would
    mis-stamp the locked pill with a different env from the one actually running."""
    from sandbox_envs.models import SandboxEnvironment, Scope

    _, raw, user = authed
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    original = await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Original", base_image="python:3.14", is_default=True
    )
    await ChatThread.objects.acreate(
        thread_id="t-existing-auto", user=user, repo_id="a/b", ref="main", sandbox_environment=original
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-existing-auto"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 200
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_explicit_env_header_does_not_emit_resolved_env(client: TestAsyncClient, authed, patched_streamer):
    """Explicit ``X-Sandbox-Env`` pick → no auto-resolution happened, so no emit. The
    locked pill already shows the picked env name client-side."""
    from sandbox_envs.models import SandboxEnvironment, Scope

    _, raw, user = authed
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    picked = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="picked", base_image="python:3.14"
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-explicit-env"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main", "X-Sandbox-Env": str(picked.id)}),
    )
    assert response.status_code == 200
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_exception_in_stream_clears_active_run_id_and_emits_run_error(client: TestAsyncClient, authed):
    _, raw, user = authed
    await ChatThread.objects.acreate(thread_id="t-boom", user=user, repo_id="a/b", ref="main")

    with (
        patch("chat.api.streaming.open_checkpointer", _mock_stream),
        patch("chat.api.streaming.set_runtime_ctx", _mock_stream),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
    ):
        m_instance = MagicMock()

        async def _boom(_input):
            if False:
                yield
            raise RuntimeError("kaboom")

        m_instance.run = _boom
        m_agent_cls.return_value = m_instance

        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-boom"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
        )

    assert response.status_code == 200
    body = response.content.decode()
    assert "RUN_ERROR" in body
    assert "run_failed" in body
    # User-facing message must not leak the raw exception class/message — that
    # could expose internal paths, SQL fragments, secrets that happen to land
    # in a stack trace.
    assert "kaboom" not in body
    assert "RuntimeError" not in body
    refreshed = await ChatThread.objects.aget(thread_id="t-boom")
    assert refreshed.active_run_id is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_thread_status_reports_active_run(client: TestAsyncClient, authed):
    _, raw, user = authed
    await ChatThread.objects.acreate(thread_id="t-live", user=user, repo_id="a/b", ref="main", active_run_id="r-1")
    await ChatThread.objects.acreate(thread_id="t-idle", user=user, repo_id="a/b", ref="main", active_run_id=None)

    live = await client.get("/chat/threads/t-live/status", headers=_auth_headers(raw))
    idle = await client.get("/chat/threads/t-idle/status", headers=_auth_headers(raw))

    assert live.status_code == 200
    assert live.json() == {"active": True}
    assert idle.status_code == 200
    assert idle.json() == {"active": False}
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_thread_status_rejects_cross_user_access(client: TestAsyncClient, authed):
    _, raw, user = authed
    other = await User.objects.acreate_user(
        username="intruder",
        email="i@example.com",
        password="x",  # noqa: S106
    )
    await ChatThread.objects.acreate(thread_id="t-foreign", user=other, repo_id="a/b", ref="main", active_run_id="r-9")

    response = await client.get("/chat/threads/t-foreign/status", headers=_auth_headers(raw))
    assert response.status_code == 404
    await user.adelete()
    await other.adelete()


@pytest.mark.django_db(transaction=True)
async def test_concurrent_run_returns_409(client: TestAsyncClient, authed):
    _, raw, user = authed
    await ChatThread.objects.acreate(
        thread_id="t-busy", user=user, repo_id="a/b", ref="main", active_run_id="r-existing"
    )
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-busy"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 409
    await user.adelete()


@pytest.fixture
def openrouter_provider(db):
    from core.models import Provider, ProviderType

    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


@pytest.mark.django_db(transaction=True)
async def test_first_turn_rejects_invalid_agent_override_and_does_not_persist(
    client: TestAsyncClient, authed, openrouter_provider
):
    """A malformed forwarded override must return 400 before any ChatThread row is created.
    Without this guard the picker validator could be bypassed and we'd persist an invalid
    spec that later fails opaquely during the stream."""
    _, raw, user = authed
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(
            threadId="t-bad-override", forwardedProps={"agent_model": "nopesuch:foo", "agent_thinking_level": ""}
        ),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 400
    assert await ChatThread.objects.filter(thread_id="t-bad-override").aexists() is False
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_divergent_override_on_existing_thread_returns_409(client: TestAsyncClient, authed, openrouter_provider):
    """Once a thread is pinned, a client supplying a different override must get a 409
    rather than silently running the persisted value — surfaces a bot bypassing the
    locked composer pill instead of letting the user think they switched models."""
    _, raw, user = authed
    await ChatThread.objects.acreate(
        thread_id="t-pinned",
        user=user,
        repo_id="a/b",
        ref="main",
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(
            threadId="t-pinned",
            forwardedProps={"agent_model": "openrouter:anthropic/claude-opus-4.6", "agent_thinking_level": "high"},
        ),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 409
    await user.adelete()


@pytest.fixture
def no_openrouter_provider(db):
    """Ensure no enabled ``openrouter`` provider exists, then bust the Provider cache."""
    from core.models import Provider

    Provider.objects.filter(slug="openrouter").delete()
    Provider.invalidate_cache()
    yield
    Provider.invalidate_cache()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_with_stale_persisted_override_returns_400(
    client: TestAsyncClient, authed, no_openrouter_provider
):
    """A thread pinned to a model whose provider was disabled after creation must
    surface a typed 400 before the stream starts, rather than blowing up deep in
    the agent with an opaque ``ValueError``."""
    _, raw, user = authed
    await ChatThread.objects.acreate(
        thread_id="t-stale-pin",
        user=user,
        repo_id="a/b",
        ref="main",
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-stale-pin"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 400
    assert "no longer available" in response.json()["detail"]
    await user.adelete()


@pytest.fixture
def disabled_openrouter_provider(db):
    """An ``openrouter`` Provider row exists but is_enabled=False — the "disabled
    not deleted" case the prior 400 path silently missed."""
    from core.models import Provider, ProviderType

    Provider.objects.filter(slug="openrouter").delete()
    Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=False
    )
    Provider.invalidate_cache()
    yield
    Provider.invalidate_cache()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_with_disabled_provider_returns_400(
    client: TestAsyncClient, authed, disabled_openrouter_provider
):
    """``is_enabled=False`` rows must also fail at the 400 boundary, not deep in
    ``BaseAgent.get_model_kwargs`` mid-run."""
    _, raw, user = authed
    await ChatThread.objects.acreate(
        thread_id="t-disabled-pin",
        user=user,
        repo_id="a/b",
        ref="main",
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-disabled-pin"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 400
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stale_pinned_model_wins_over_divergent_client_override(
    client: TestAsyncClient, authed, no_openrouter_provider
):
    """When a thread is both pinned to a stale model AND the client sends a
    divergent override, the 400 "start a new thread" must fire first — otherwise
    the 409 traps the user in a thread that cannot be run either way.

    Forwarded override carries only ``agent_thinking_level`` so it validates
    against the live Provider table (model is empty); the divergence then comes
    from the level mismatch with the persisted ``low``.
    """
    _, raw, user = authed
    await ChatThread.objects.acreate(
        thread_id="t-stale-and-divergent",
        user=user,
        repo_id="a/b",
        ref="main",
        agent_model="openrouter:anthropic/claude-haiku-4.5",
        agent_thinking_level="low",
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(
            threadId="t-stale-and-divergent", forwardedProps={"agent_model": "", "agent_thinking_level": "high"}
        ),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 400
    await user.adelete()
