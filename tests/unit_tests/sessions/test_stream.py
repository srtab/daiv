"""Tests for SessionStreamView (SSE endpoint for Run status updates)."""

from __future__ import annotations

import uuid

from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="streamuser",
        email="streamuser@test.com",
        password="testpass123",  # noqa: S106
    )


def _create_session(user=None, **kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
    }
    if user is not None:
        defaults["user"] = user
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.RUNNING,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


@pytest.mark.django_db
class TestSessionStreamView:
    def test_unauthenticated_returns_403(self, client):
        """Unauthenticated requests get 403."""
        from django.test import Client

        anon = Client()
        resp = anon.get(reverse("session_stream"), {"ids": str(uuid.uuid4())})
        assert resp.status_code == 403

    def test_missing_ids_returns_400(self, logged_in_client):
        resp = logged_in_client.get(reverse("session_stream"))
        assert resp.status_code == 400

    def test_invalid_uuids_only_returns_400(self, logged_in_client):
        """If all id values are invalid UUIDs, return 400."""
        resp = logged_in_client.get(reverse("session_stream"), {"ids": "not-a-uuid"})
        assert resp.status_code == 400

    def test_valid_ids_returns_sse_stream(self, logged_in_client, user, db):
        """Valid UUIDs produce a streaming SSE response (200, text/event-stream)."""
        session = _create_session(user=user)
        run = _create_run(session, user=user, status=RunStatus.SUCCESSFUL)
        resp = logged_in_client.get(reverse("session_stream"), {"ids": str(run.id)})
        assert resp.status_code == 200
        assert "text/event-stream" in resp.get("Content-Type", "")

    def test_stream_route_precedes_slug_catchall(self):
        """session_stream URL resolves to SessionStreamView, not SessionDetailView."""
        from django.urls import resolve

        from sessions.views import SessionStreamView

        match = resolve(reverse("session_stream"))
        assert match.func.view_class is SessionStreamView


@pytest.fixture
def logged_in_client(user):
    from django.test import Client

    c = Client()
    c.force_login(user)
    return c
