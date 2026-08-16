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


class TestSessionMergeRequestRedirect:
    """The URL DAIV writes into merge request descriptions resolves to every session on that MR."""

    def _get(self, client, session):
        return client.get(reverse("session_merge_request", kwargs={"thread_id": session.thread_id}))

    def test_redirects_to_the_mr_filtered_session_list(self, client, session_fixture):
        session_fixture.merge_request_iid = 42
        session_fixture.save(update_fields=["merge_request_iid"])

        resp = self._get(client, session_fixture)

        assert resp.status_code == 302
        assert resp["Location"] == f"{reverse('session_list')}?repo=group%2Fproject&mr=42"

    def test_resolves_the_iid_from_a_run_when_the_session_lacks_one(self, client, session_fixture, run_fixture):
        """Issue-scope sessions only learn their MR when the run backfills it at completion."""
        run_fixture.merge_request_iid = 7
        run_fixture.save(update_fields=["merge_request_iid"])

        resp = self._get(client, session_fixture)

        assert resp["Location"].endswith("mr=7")

    def test_falls_back_to_the_transcript_before_the_iid_is_known(self, client, session_fixture):
        """A reviewer clicking before the run finishes still lands somewhere useful."""
        resp = self._get(client, session_fixture)

        assert resp["Location"] == reverse("session_detail", kwargs={"thread_id": session_fixture.thread_id})

    def test_redirect_is_temporary(self, client, session_fixture):
        """A 301 would let browsers cache the pre-backfill fallback forever."""
        assert self._get(client, session_fixture).status_code == 302

    def test_unknown_thread_returns_404(self, client):
        resp = client.get(reverse("session_merge_request", kwargs={"thread_id": "nope"}))

        assert resp.status_code == 404

    def test_hidden_from_users_who_cannot_see_the_session(self, session_fixture, other_user):
        c = Client()
        c.force_login(other_user)

        assert self._get(c, session_fixture).status_code == 404

    def test_requires_login(self, session_fixture):
        resp = self._get(Client(), session_fixture)

        assert resp.status_code == 302
        assert "login" in resp["Location"].lower()
