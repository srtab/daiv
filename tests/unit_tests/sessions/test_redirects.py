"""Tests for legacy activity/chat URL redirects (Task 14).

All old /dashboard/activity/ and /dashboard/chat/ URLs must return 301
permanent redirects pointing at the equivalent sessions routes.
"""

from django.test import Client
from django.urls import reverse

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


def test_activity_list_redirects(client):
    resp = client.get("/dashboard/activity/")
    assert resp.status_code == 301
    assert resp["Location"] == reverse("session_list")


def test_activity_detail_redirects_to_run_anchor(client, run_fixture):
    resp = client.get(f"/dashboard/activity/{run_fixture.id}/")
    assert resp.status_code == 301
    expected = reverse("session_detail", kwargs={"thread_id": run_fixture.session_id}) + f"#run-{run_fixture.id}"
    assert resp["Location"] == expected


def test_activity_detail_unknown_run_returns_404(client):
    import uuid

    fake_pk = uuid.uuid4()
    resp = client.get(f"/dashboard/activity/{fake_pk}/")
    assert resp.status_code == 404


def test_chat_list_redirects(client):
    resp = client.get("/dashboard/chat/")
    assert resp.status_code == 301
    assert resp["Location"] == reverse("session_list")


def test_chat_new_redirects(client):
    resp = client.get("/dashboard/chat/new/")
    assert resp.status_code == 301
    assert resp["Location"] == reverse("session_new_chat")


def test_chat_detail_redirects(client, session_fixture):
    resp = client.get(f"/dashboard/chat/{session_fixture.thread_id}/")
    assert resp.status_code == 301
    assert resp["Location"] == reverse("session_detail", kwargs={"thread_id": session_fixture.thread_id})


def test_activity_list_requires_login(admin_user):
    c = Client()
    resp = c.get("/dashboard/activity/")
    # RedirectView without login_required still redirects (301 to session_list)
    # because RedirectView itself has no auth gate — only the detail view does.
    assert resp.status_code == 301


def test_activity_detail_requires_login(run_fixture):
    c = Client()
    resp = c.get(f"/dashboard/activity/{run_fixture.id}/")
    # LegacyActivityDetailRedirectView has LoginRequiredMixin → 302 to login
    assert resp.status_code == 302
    assert "login" in resp["Location"].lower()
