"""Tests for sessions.api.views — the session_turns endpoint."""

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
# auth gate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_session_turns_rejects_anonymous_and_bad_token(db):
    """``session_turns`` sits behind AuthBearer/django_auth — anonymous or bad-token callers
    get 401 and never reach the session lookup (no transcript leak). Exercised through the
    real Django client so the SessionMiddleware ``django_auth`` needs is present (the ninja
    TestAsyncClient's mock request has no session)."""
    from django.test import Client
    from django.urls import reverse

    anon = Client()
    url = reverse("api:session_turns", kwargs={"thread_id": str(uuid.uuid4())})
    assert anon.get(url).status_code == 401, "anonymous should be 401"
    assert anon.get(url, HTTP_AUTHORIZATION="Bearer not-a-real-key").status_code == 401, "bad token should be 401"


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


@pytest.mark.django_db(transaction=True)
async def test_session_turns_404_for_other_users_session(client, authed):
    """turns goes through the same by_owner scoping as status — a stranger's thread_id
    must 404 (no re-hydrated transcript leak), and ahydrate_thread is never reached."""
    _key_obj, raw, _user = authed
    other = await User.objects.acreate_user(
        username="other-turns",
        email="other-turns@example.com",
        password="x",  # noqa: S106
    )
    session_other = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", ref="main", user=other
    )

    hydrate = AsyncMock(return_value=([], False, None))
    with patch("sessions.api.views.ahydrate_thread", hydrate):
        resp = await client.get(f"/sessions/{session_other.thread_id}/turns", headers=_auth_headers(raw))

    assert resp.status_code == 404
    hydrate.assert_not_called()
