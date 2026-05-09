"""Tests for the repoless chat API path (no X-Repo-ID / X-Ref headers).

Design choice: we test both layers:
1. The view layer via TestAsyncClient to verify the mixed-header 400 contract.
2. The service layer via ChatThreadService.get_or_create_for_user directly for
   the repoless thread-creation contract, rather than trying to fully mock the
   streaming pipeline. This isolates the service-layer invariant (repo_id=None,
   ref=None on the resulting ChatThread) without coupling the test to the
   internals of ChatRunStreamer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from ninja.testing import TestAsyncClient

from accounts.models import APIKey, User
from chat.api.threads import ChatThreadService
from chat.models import ChatThread
from daiv.api import api

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
async def authed():
    """Return (APIKey, raw_key, user) for authenticated tests."""
    user = await User.objects.acreate_user(
        username="repoless-user",
        email="repoless@example.com",
        password="testpass123",  # noqa: S106
    )
    key_obj, raw = await APIKey.objects.create_key(user=user, name="RepolessTest")
    return key_obj, raw, user


def _auth_headers(raw_key: str, **extra) -> dict:
    return {"Authorization": f"Bearer {raw_key}", **extra}


def _run_agent_input(**overrides) -> dict:
    return {
        "threadId": "t-repoless-1",
        "runId": "r-repoless-1",
        "state": {},
        "messages": [{"id": "m-1", "role": "user", "content": "hello without repo"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        **overrides,
    }


def _mock_ctx(*_args, **_kwargs):
    """Async context manager mock used to neutralise open_checkpointer / set_runtime_ctx."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# ---------------------------------------------------------------------------
# View-layer: mixed-header contract
# ---------------------------------------------------------------------------


@pytest.mark.django_db
async def test_mixed_headers_only_repo_id_returns_400(client: TestAsyncClient, authed):
    """Sending X-Repo-ID without X-Ref must return 400, not 404."""
    _, raw, user = authed
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-mixed-1"),
        headers=_auth_headers(raw, **{"X-Repo-ID": "owner/repo"}),
    )
    assert response.status_code == 400
    await user.adelete()


@pytest.mark.django_db
async def test_mixed_headers_only_ref_returns_400(client: TestAsyncClient, authed):
    """Sending X-Ref without X-Repo-ID must return 400, not 404."""
    _, raw, user = authed
    response = await client.post(
        "/chat/completions",
        json=_run_agent_input(threadId="t-mixed-2"),
        headers=_auth_headers(raw, **{"X-Ref": "main"}),
    )
    assert response.status_code == 400
    await user.adelete()


# ---------------------------------------------------------------------------
# View-layer: repoless path creates a thread and returns 200 streaming
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_repoless_request_creates_thread_with_null_repo(client: TestAsyncClient, authed):
    """No X-Repo-ID and no X-Ref headers → thread created with repo_id=None, ref=None."""
    _, raw, user = authed
    with (
        patch("chat.api.streaming.open_checkpointer", _mock_ctx),
        patch("chat.api.streaming.set_runtime_ctx", _mock_ctx),
        patch("chat.api.streaming.create_daiv_agent", new=AsyncMock()),
        patch("chat.api.streaming.RuntimeContextLangGraphAGUIAgent") as m_agent_cls,
    ):
        m_instance = MagicMock()

        async def _empty_stream(_input):
            if False:  # noqa: SIM210 — generator that yields nothing
                yield

        m_instance.run = _empty_stream
        m_agent_cls.return_value = m_instance

        response = await client.post(
            "/chat/completions",
            json=_run_agent_input(threadId="t-repoless-view-1"),
            headers=_auth_headers(raw),  # no repo headers
        )

    assert response.status_code == 200
    created = await ChatThread.objects.filter(thread_id="t-repoless-view-1").afirst()
    assert created is not None
    assert created.user_id == user.id
    assert created.repo_id is None
    assert created.ref is None
    assert created.active_run_id is None
    await user.adelete()


# ---------------------------------------------------------------------------
# Service-layer: get_or_create_for_user with repo_id=None, ref=None
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_service_creates_repoless_thread():
    """get_or_create_for_user with repo_id=None, ref=None persists both as NULL."""
    from types import SimpleNamespace

    user = await User.objects.acreate_user(
        username="svc-repoless",
        email="svc-repoless@example.com",
        password="x",  # noqa: S106
    )
    input_data = SimpleNamespace(messages=[SimpleNamespace(role="user", content="hello")])

    thread = await ChatThreadService.get_or_create_for_user(
        user=user, thread_id="t-svc-repoless-1", repo_id=None, ref=None, input_data=input_data
    )

    assert thread.repo_id is None
    assert thread.ref is None
    assert thread.user_id == user.id

    # Verify DB row, not just the in-memory object.
    from_db = await ChatThread.objects.aget(thread_id="t-svc-repoless-1")
    assert from_db.repo_id is None
    assert from_db.ref is None
    await user.adelete()


@pytest.mark.django_db(transaction=True)
async def test_service_skips_title_task_for_repoless_thread():
    """Title-generation task must NOT be enqueued when repo_id is None."""
    from types import SimpleNamespace

    user = await User.objects.acreate_user(
        username="svc-repoless-title",
        email="svc-repoless-title@example.com",
        password="x",  # noqa: S106
    )
    input_data = SimpleNamespace(messages=[SimpleNamespace(role="user", content="a real message")])

    with patch("chat.api.threads.generate_title_task") as mock_task:
        await ChatThreadService.get_or_create_for_user(
            user=user, thread_id="t-svc-repoless-title", repo_id=None, ref=None, input_data=input_data
        )
        mock_task.aenqueue.assert_not_called()

    await user.adelete()
