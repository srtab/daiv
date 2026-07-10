import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ninja.testing import TestAsyncClient
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import APIKey, User
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
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-owned", user=other, repo_id="a/b", ref="main")

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-owned"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 403
    await user.adelete()
    await other.adelete()


@pytest.mark.django_db(transaction=True)
async def test_unknown_thread_id_implicit_creates_thread(client: TestAsyncClient, authed, fake_redis, captured_runs):
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
        await asyncio.gather(*captured_runs)

    created = await Session.objects.filter(thread_id="t-new").afirst()
    assert created is not None
    assert created.user_id == user.id
    assert created.repo_id == "a/b"
    assert created.ref == "main"
    # The finally block in ChatRunStreamer.events() clears the run slot after the stream completes.
    assert created.active_run_id is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_first_message_auto_resolves_user_env_onto_thread(
    client: TestAsyncClient, authed, fake_redis, captured_runs
):
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
        await asyncio.gather(*captured_runs)

    created = await Session.objects.aget(thread_id="t-auto")
    assert created.sandbox_environment_id == user_env.id
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_keeps_original_env_even_when_resolution_would_pick_another(
    client: TestAsyncClient, authed, fake_redis, captured_runs
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
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
        thread_id="t-keep",
        user=user,
        repo_id="a/b",
        ref="main",
        sandbox_environment=original,
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
        await asyncio.gather(*captured_runs)

    thread = await Session.objects.aget(thread_id="t-keep")
    assert thread.sandbox_environment_id == original.id
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_auto_resolved_env_passed_to_streamer_on_first_turn(
    client: TestAsyncClient, authed, fake_redis, captured_runs, patched_streamer
):
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
    await asyncio.gather(*captured_runs)
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] == {
        "id": str(user_env.id),
        "name": "mine",
        "scope": "user",
    }
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_existing_thread_auto_submit_does_not_emit_resolved_env(
    client: TestAsyncClient, authed, fake_redis, captured_runs, patched_streamer
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
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
        thread_id="t-existing-auto",
        user=user,
        repo_id="a/b",
        ref="main",
        sandbox_environment=original,
    )

    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-existing-auto"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )
    assert response.status_code == 200
    await asyncio.gather(*captured_runs)
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_explicit_env_header_does_not_emit_resolved_env(
    client: TestAsyncClient, authed, fake_redis, captured_runs, patched_streamer
):
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
    await asyncio.gather(*captured_runs)
    assert patched_streamer.call_args.kwargs["auto_resolved_env"] is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_exception_in_stream_clears_active_run_id_and_emits_run_error(
    client: TestAsyncClient, authed, fake_redis, captured_runs
):
    _, raw, user = authed
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-boom", user=user, repo_id="a/b", ref="main")

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
        assert response.json()["run_id"] == "r-1"
        await asyncio.gather(*captured_runs)

    refreshed = await Session.objects.aget(thread_id="t-boom")
    assert refreshed.active_run_id is None

    from chat.api import relay as relay_mod

    published = "".join(
        fields.get("data", "") for _id, fields in fake_redis.streams[relay_mod.run_events_key("t-boom", "r-1")]
    )
    assert "RUN_ERROR" in published
    assert "run_failed" in published
    # User-facing message must not leak the raw exception class/message.
    assert "kaboom" not in published
    assert "RuntimeError" not in published

    # ...and neither may the *stored* reason on the Run row (which the timeline renders
    # verbatim). The real finalize_chat_run runs here (not patched), so this asserts the
    # DB→timeline surface stays sanitized end-to-end, not just the SSE stream.
    failed_run = await Run.objects.aget(session_id="t-boom", status=RunStatus.FAILED)
    assert failed_run.error_message == "Run failed. Check server logs for details."
    assert "kaboom" not in failed_run.error_message
    assert "RuntimeError" not in failed_run.error_message
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_concurrent_run_returns_409(client: TestAsyncClient, authed):
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-busy", user=user, repo_id="a/b", ref="main", active_run_id="r-existing"
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
    """A malformed forwarded override must return 400 before any Session row is created.
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
    assert await Session.objects.filter(thread_id="t-bad-override").aexists() is False
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_divergent_override_on_existing_thread_returns_409(client: TestAsyncClient, authed, openrouter_provider):
    """Once a thread is pinned, a client supplying a different override must get a 409
    rather than silently running the persisted value — surfaces a bot bypassing the
    locked composer pill instead of letting the user think they switched models."""
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
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
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
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
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
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
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT,
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


@pytest.mark.django_db(transaction=True)
async def test_completion_denied_repo_returns_404_and_no_thread(client: TestAsyncClient, authed):
    from codebase.authorization import RepositoryAccessDenied

    _, raw, user = authed
    thread_id = str(uuid.uuid4())
    body = _run_agent_input(threadId=thread_id)

    with patch("chat.api.views.aassert_can_run", new=AsyncMock(side_effect=RepositoryAccessDenied(["group/repo"]))):
        response = await client.post(
            "/chat/completions", json=body, headers=_auth_headers(raw, **{"X-Repo-ID": "group/repo", "X-Ref": "main"})
        )

    assert response.status_code == 404
    assert not await Session.objects.filter(thread_id=thread_id).aexists()
    await user.adelete()


# ---------------------------------------------------------------------------
# GET /chat/stream — SSE replay + tail of a run's relay stream
# ---------------------------------------------------------------------------


async def _seed_run_events(fake_redis, thread_id, run_id, payloads, *, end=True):
    from chat.api import relay

    for p in payloads:
        await relay.publish_event(thread_id, run_id, p)
    if end:
        await relay.publish_end(thread_id, run_id)


@pytest.mark.django_db(transaction=True)
async def test_stream_replays_all_events_and_ends(client: TestAsyncClient, authed, fake_redis):
    _, raw, user = authed
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-s1", user=user, repo_id="a/b", ref="main")
    await _seed_run_events(fake_redis, "t-s1", "r-1", ['{"type":"RUN_STARTED"}', '{"type":"RUN_FINISHED"}'])

    response = await client.get("/chat/stream?thread_id=t-s1&run_id=r-1", headers=_auth_headers(raw))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    body = response.content.decode()
    assert body.startswith("retry: 2000\n\n")
    assert 'data: {"type":"RUN_STARTED"}' in body
    assert 'data: {"type":"RUN_FINISHED"}' in body
    # every data frame carries a resumable id
    assert "id: 1-0" in body
    assert 'event: end\ndata: {"reason": "finished"}' in body
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stream_resumes_after_last_event_id(client: TestAsyncClient, authed, fake_redis):
    _, raw, user = authed
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-s2", user=user, repo_id="a/b", ref="main")
    await _seed_run_events(fake_redis, "t-s2", "r-1", ['{"n":1}', '{"n":2}', '{"n":3}'])

    response = await client.get(
        "/chat/stream?thread_id=t-s2&run_id=r-1", headers=_auth_headers(raw, **{"Last-Event-ID": "2-0"})
    )

    body = response.content.decode()
    assert '{"n":1}' not in body
    assert '{"n":2}' not in body
    assert '{"n":3}' in body
    assert 'event: end\ndata: {"reason": "finished"}' in body
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stream_drains_tail_and_ends_when_slot_released_without_sentinel(
    client: TestAsyncClient, authed, fake_redis
):
    """Writer died after releasing the lock but before the sentinel (or the
    sentinel expired): replay what exists, then synthesize a finished end."""
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-s3", user=user, repo_id="a/b", ref="main", active_run_id=None
    )
    await _seed_run_events(fake_redis, "t-s3", "r-1", ['{"n":1}'], end=False)

    response = await client.get("/chat/stream?thread_id=t-s3&run_id=r-1", headers=_auth_headers(raw))

    body = response.content.decode()
    assert '{"n":1}' in body
    assert 'event: end\ndata: {"reason": "finished"}' in body
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stream_reports_stale_when_holder_heartbeat_is_dead(client: TestAsyncClient, authed, fake_redis):
    from datetime import timedelta

    from django.utils import timezone

    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-s4", user=user, repo_id="a/b", ref="main", active_run_id="r-1"
    )
    await Session.objects.filter(thread_id="t-s4").aupdate(last_active_at=timezone.now() - timedelta(minutes=31))

    response = await client.get("/chat/stream?thread_id=t-s4&run_id=r-1", headers=_auth_headers(raw))

    body = response.content.decode()
    assert 'event: end\ndata: {"reason": "stale"}' in body
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stream_rejects_thread_not_owned(client: TestAsyncClient, authed, fake_redis):
    _, raw, user = authed
    other = await User.objects.acreate_user(username="sowner", email="s@example.com", password="x")  # noqa: S106
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-s5", user=other, repo_id="a/b", ref="main")

    response = await client.get("/chat/stream?thread_id=t-s5&run_id=r-1", headers=_auth_headers(raw))

    assert response.status_code == 404
    await user.adelete()
    await other.adelete()


class _ScriptedRedis:
    """Minimal stream client that honors Last-Event-ID and reveals appended
    entries across successive ``xread`` calls, so a test can drive the live-tail
    loop through multiple blocking iterations deterministically. ``reveal`` maps
    an xread call index → entries to make visible *before* serving that call; an
    absent index serves nothing new (the stand-in for a block timeout).

    This is what the shared ``FakeAsyncRedis`` (which returns everything at once)
    cannot do: exercise the loop re-entering with an advanced ``last_id`` while a
    writer is still producing.
    """

    def __init__(self, key: str, reveal: dict[int, list[tuple[str, dict]]]):
        self._key = key
        self._reveal = reveal
        self._visible: list[tuple[str, dict]] = []
        self._calls = 0

    async def xread(self, streams, count=None, block=None):
        await asyncio.sleep(0)
        self._visible.extend(self._reveal.get(self._calls, []))
        self._calls += 1
        last_id = streams[self._key]
        newer = [e for e in self._visible if self._after(e[0], last_id)]
        if count:
            newer = newer[:count]
        return [(self._key, newer)] if newer else []

    @staticmethod
    def _after(entry_id: str, last_id: str) -> bool:
        def parse(i: str) -> tuple[int, int]:
            a, _, b = i.partition("-")
            return (int(a), int(b or 0))

        return parse(entry_id) > parse(last_id)


@pytest.mark.django_db(transaction=True)
async def test_stream_emits_error_frame_when_relay_read_fails(client: TestAsyncClient, authed):
    """A relay/backend error mid-tail must emit an explicit ``error`` end frame
    rather than aborting the response unframed — an unframed abort is
    indistinguishable from a transient drop, so EventSource would reconnect
    forever against a still-broken backend.
    """
    _, raw, user = authed
    await Session.objects.acreate(origin=SessionOrigin.CHAT, thread_id="t-err", user=user, repo_id="a/b", ref="main")
    broken = MagicMock()
    broken.xread = AsyncMock(side_effect=RuntimeError("redis down"))

    with patch("chat.api.relay.get_redis", return_value=broken):
        response = await client.get("/chat/stream?thread_id=t-err&run_id=r-1", headers=_auth_headers(raw))

    body = response.content.decode()
    assert 'event: end\ndata: {"reason": "error"}' in body
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_stream_emits_keepalive_and_resumes_tail_across_reads(client: TestAsyncClient, authed):
    """Drive the live-tail loop across multiple reads: a data read, a quiet read
    (holder live + fresh → keep-alive), then a final read with new data + the
    sentinel. Proves Last-Event-ID advances (an already-sent event is not
    re-sent) and the keep-alive branch fires — neither is reachable via the
    all-at-once ``FakeAsyncRedis``.
    """
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-live", user=user, repo_id="a/b", ref="main", active_run_id="r-1"
    )
    from chat.api import relay

    key = relay.run_events_key("t-live", "r-1")
    scripted = _ScriptedRedis(
        key, {0: [("1-0", {"data": '{"n":1}'})], 2: [("2-0", {"data": '{"n":2}'}), ("3-0", {"end": "1"})]}
    )

    with patch("chat.api.relay.get_redis", return_value=scripted):
        response = await client.get("/chat/stream?thread_id=t-live&run_id=r-1", headers=_auth_headers(raw))

    body = response.content.decode()
    assert body.count('{"n":1}') == 1  # not re-sent after last_id advanced past it
    assert body.count('{"n":2}') == 1
    assert ": keep-alive" in body
    assert 'event: end\ndata: {"reason": "finished"}' in body
    await user.adelete()


class _QuietSlowRedis:
    """``xread`` always times out empty after a short real delay, so the live-tail
    loop iterates (emitting keep-alives) until the duration cap trips against real
    monotonic time — no global clock patching (which would corrupt asyncio's own
    ``loop.time()``).
    """

    async def xread(self, streams, count=None, block=None):
        await asyncio.sleep(0.02)
        return []


@pytest.mark.django_db(transaction=True)
async def test_stream_closes_without_end_frame_at_duration_cap(client: TestAsyncClient, authed):
    """Hitting the duration cap closes the response WITHOUT an end frame (the
    browser reconnects with Last-Event-ID). Assert no terminal frame is emitted
    even though the holder is still live and fresh.
    """
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-cap", user=user, repo_id="a/b", ref="main", active_run_id="r-1"
    )

    with (
        patch("chat.api.relay.get_redis", return_value=_QuietSlowRedis()),
        patch("chat.api.views.STREAM_MAX_DURATION_S", 0.05),
    ):
        response = await client.get("/chat/stream?thread_id=t-cap&run_id=r-1", headers=_auth_headers(raw))

    body = response.content.decode()
    assert ": keep-alive" in body  # at least one quiet iteration ran before the cap
    assert "event: end" not in body  # cap close is silent on purpose
    await user.adelete()


# ---------------------------------------------------------------------------
# POST /chat/completions — spawn semantics
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_completions_returns_run_handle_json_by_default(
    client: TestAsyncClient, authed, fake_redis, captured_runs, patched_streamer
):
    _, raw, user = authed
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-json"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
    )

    assert response.status_code == 200
    assert response.json() == {"run_id": "r-1", "thread_id": "t-json"}
    await asyncio.gather(*captured_runs)
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_completions_streams_inline_for_event_stream_accept(
    client: TestAsyncClient, authed, fake_redis, captured_runs, patched_streamer
):
    """AG-UI protocol compatibility: an Accept: text/event-stream caller still
    gets SSE from the POST itself — but the run executes in the spawned task,
    so this response is just a relay tail."""
    _, raw, user = authed
    # Configure the mock streamer instance so run_to_relay publishes to the
    # same relay key that _run_event_frames reads from.
    patched_streamer.return_value.thread_id = "t-sse"
    patched_streamer.return_value.run_id = "r-1"
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-sse"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main", "Accept": "text/event-stream"}),
    )

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    body = response.content.decode()
    assert 'event: end\ndata: {"reason": "finished"}' in body
    await asyncio.gather(*captured_runs)
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_completion_invalid_env_header_returns_400_and_no_thread(client: TestAsyncClient, authed):
    """An ``X-Sandbox-Env`` naming an env the user can't use raises ``LookupError``
    from env resolution → 400, before any Session is created or run spawned."""
    from django.core.cache import cache

    _, raw, user = authed
    # The per-user job throttle keys on the (constant) test username, so completion
    # POSTs share one bucket across the file — reset it so this test isn't throttled.
    await cache.aclear()
    with patch("chat.api.views.resolve_env_for_user", new=AsyncMock(side_effect=LookupError("unknown env"))):
        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-badenv"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main", "X-Sandbox-Env": "nope"}),
        )

    assert response.status_code == 400
    assert not await Session.objects.filter(thread_id="t-badenv").aexists()
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_completion_no_resolvable_model_returns_400(client: TestAsyncClient, authed, fake_redis):
    """The submit-time model gate: when no agent model can be resolved (client sent
    no override and no system default exists), ``ensure_agent_model_available``
    raises → 400 rather than exploding mid-stream."""
    from django.core.cache import cache

    from automation.agent.validators import AgentOverrideError

    _, raw, user = authed
    await cache.aclear()  # isolate from the shared per-username job-throttle bucket
    with patch(
        "chat.api.views.ensure_agent_model_available", side_effect=AgentOverrideError("no agent model configured")
    ):
        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-nomodel"),
            headers=_auth_headers(raw, **{"X-Repo-ID": "a/b", "X-Ref": "main"}),
        )

    assert response.status_code == 400
    await user.adelete()


# ---------------------------------------------------------------------------
# POST /chat/cancel
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_cancel_sets_flag_and_reports_local_miss(client: TestAsyncClient, authed, fake_redis):
    from chat.api import relay as relay_mod

    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-c1", user=user, repo_id="a/b", ref="main", active_run_id="r-9"
    )

    response = await client.post(
        "/chat/cancel", json={"thread_id": "t-c1", "run_id": "r-9"}, headers=_auth_headers(raw)
    )

    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "local": False}
    assert await relay_mod.cancel_requested("t-c1", "r-9") is True
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_cancel_rejects_run_not_in_flight(client: TestAsyncClient, authed, fake_redis):
    _, raw, user = authed
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-c2", user=user, repo_id="a/b", ref="main", active_run_id="r-current"
    )

    response = await client.post(
        "/chat/cancel", json={"thread_id": "t-c2", "run_id": "r-stale"}, headers=_auth_headers(raw)
    )

    assert response.status_code == 409
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_cancel_rejects_foreign_thread(client: TestAsyncClient, authed, fake_redis):
    _, raw, user = authed
    other = await User.objects.acreate_user(username="cowner", email="c@example.com", password="x")  # noqa: S106
    await Session.objects.acreate(
        origin=SessionOrigin.CHAT, thread_id="t-c3", user=other, repo_id="a/b", ref="main", active_run_id="r-1"
    )

    response = await client.post(
        "/chat/cancel", json={"thread_id": "t-c3", "run_id": "r-1"}, headers=_auth_headers(raw)
    )

    assert response.status_code == 404
    await user.adelete()
    await other.adelete()
