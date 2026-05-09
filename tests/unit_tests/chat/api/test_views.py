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


@pytest.mark.django_db
async def test_missing_repo_id_header_returns_400(client: TestAsyncClient, authed):
    _, raw, user = authed
    response = await client.post(
        "/chat/completions", json=_run_agent_input(), headers=_auth_headers(raw, **{"X-Ref": "main"})
    )
    assert response.status_code == 400
    await user.adelete()


@pytest.mark.django_db
async def test_missing_ref_header_returns_400(client: TestAsyncClient, authed):
    _, raw, user = authed
    response = await client.post(
        "/chat/completions", json=_run_agent_input(), headers=_auth_headers(raw, **{"X-Repo-ID": "owner/repo"})
    )
    assert response.status_code == 400
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
