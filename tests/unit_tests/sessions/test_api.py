"""Tests for sessions.api.views — session_status and session_turns endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from ninja.testing import TestAsyncClient
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import APIKey, User
from daiv.api import api


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
async def authed(db):
    """Return (APIKey, raw_key, user) for authenticated tests."""
    user = await User.objects.acreate_user(
        username="sessuser",
        email="sessuser@example.com",
        password="testpass123",  # noqa: S106
    )
    key_obj, raw = await APIKey.objects.create_key(user=user, name="Test")
    return key_obj, raw, user


def _auth_headers(raw_key: str) -> dict:
    return {"Authorization": f"Bearer {raw_key}"}


def _create_session(user=None, **kwargs) -> Session:
    defaults = {"thread_id": str(uuid.uuid4()), "origin": SessionOrigin.CHAT, "repo_id": "group/project", "ref": "main"}
    if user is not None:
        defaults["user"] = user
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.CHAT,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


# ---------------------------------------------------------------------------
# session_status
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_session_status_active_flag(client, authed):
    """Session with active_run_id -> {"active": true}; without -> false; other user's -> 404."""
    _key_obj, raw, user = authed

    # Session with active_run_id set -> active: true
    session_active = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id="run-abc-123",
    )

    resp = await client.get(f"/sessions/{session_active.thread_id}/status", headers=_auth_headers(raw))
    assert resp.status_code == 200
    assert resp.json() == {"active": True}

    # Session without active_run_id -> active: false
    session_idle = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )
    resp = await client.get(f"/sessions/{session_idle.thread_id}/status", headers=_auth_headers(raw))
    assert resp.status_code == 200
    assert resp.json() == {"active": False}

    # Other user's session -> 404
    other = await User.objects.acreate_user(
        username="other-sess",
        email="other-sess@example.com",
        password="x",  # noqa: S106
    )
    session_other = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", ref="main", user=other
    )
    resp = await client.get(f"/sessions/{session_other.thread_id}/status", headers=_auth_headers(raw))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# session_turns
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_session_turns_returns_built_turns(client, authed):
    """Patch ahydrate_thread to return two fake messages; response contains build_turns
    output and expired=False."""
    from langchain_core.messages import AIMessage, HumanMessage

    _key_obj, raw, user = authed

    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )

    fake_messages = [HumanMessage(content="hello", id="m-1"), AIMessage(content="world", id="m-2")]

    with patch("sessions.api.views.ahydrate_thread", AsyncMock(return_value=(fake_messages, False, None))):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    data = resp.json()
    assert data["expired"] is False
    assert data["active"] is False
    # build_turns should produce one user turn and one assistant turn
    roles = [t["role"] for t in data["turns"]]
    assert "user" in roles
    assert "assistant" in roles


@pytest.mark.django_db(transaction=True)
async def test_session_turns_expired(client, authed):
    """ahydrate_thread returns (.., True, ..) -> {"turns": [], "expired": true, "active": false}."""
    _key_obj, raw, user = authed

    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=None,
    )

    with patch("sessions.api.views.ahydrate_thread", AsyncMock(return_value=([], True, None))):
        resp = await client.get(f"/sessions/{session.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 200
    data = resp.json()
    assert data["expired"] is True
    assert data["turns"] == []
    assert data["active"] is False
