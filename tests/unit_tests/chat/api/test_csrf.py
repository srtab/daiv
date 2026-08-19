"""Regression test: session-authenticated ninja routes must enforce CSRF.

django-ninja 1.x marks every view ``csrf_exempt`` at the Django middleware
level (see ``ninja/operation.py``) and instead delegates CSRF enforcement to
the cookie/session auth classes — ``SessionAuth`` (a.k.a. ``django_auth``) runs
``ninja.utils.check_csrf`` on every request it handles. Bearer/API-key clients
are unaffected: they authenticate via ``HttpBearer``/``APIKeyHeader``, which
never touch CSRF.

This pins both halves on a representative session-auth route (``POST
/api/chat/cancel``): a cookie-authenticated POST without the token is rejected,
and one carrying the matching token reaches the handler.
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret
from django.test import Client

import pytest
from sessions.models import Session, SessionOrigin

from accounts.models import User

CANCEL_URL = "/api/chat/cancel"


def _seed_csrf(client: Client) -> str:
    """Plant a CSRF secret in the session (CSRF_USE_SESSIONS=True) and return a
    valid masked token the middleware will accept in ``X-CSRFToken``."""
    session = client.session
    secret = session.get("_csrftoken") or _get_new_csrf_string()
    session["_csrftoken"] = secret
    session.save()
    return _mask_cipher_secret(secret)


@pytest.fixture
def session_user(db):
    user = User.objects.create_user(
        username="csrfuser",
        email="csrf@example.com",
        password="testpass123",  # noqa: S106
    )
    thread_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    Session.objects.create(
        thread_id=thread_id,
        origin=SessionOrigin.CHAT,
        repo_id="group/project",
        ref="main",
        user=user,
        active_run_id=run_id,
    )
    return user, thread_id, run_id


@pytest.mark.django_db
def test_session_post_without_csrf_token_is_rejected(session_user):
    user, thread_id, run_id = session_user
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = client.post(
        CANCEL_URL, data=json.dumps({"thread_id": thread_id, "run_id": run_id}), content_type="application/json"
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_session_post_with_csrf_token_succeeds(session_user):
    user, thread_id, run_id = session_user
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    token = _seed_csrf(client)

    with (
        patch("chat.api.relay.RunRelay.request_cancel", new=AsyncMock()),
        patch("chat.api.runner.supervisor.cancel_local", return_value=False),
    ):
        response = client.post(
            CANCEL_URL,
            data=json.dumps({"thread_id": thread_id, "run_id": run_id}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    assert response.status_code == 200
    assert response.json() == {"cancelled": True, "local": False}
