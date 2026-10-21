from django.test import Client
from django.urls import reverse

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


def _several_requests(session, run):
    session.merge_request_iid = 42
    session.save(update_fields=["merge_request_iid"])
    run.merge_request_iid = 99
    run.save(update_fields=["merge_request_iid"])


def _no_repository(session, run):
    session.merge_request_iid = 42
    session.repo_id = ""
    session.save(update_fields=["merge_request_iid", "repo_id"])


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

    @pytest.mark.parametrize(
        "setup",
        [
            pytest.param(lambda session, run: None, id="before-backfill"),
            pytest.param(_several_requests, id="several-requests"),
            pytest.param(_no_repository, id="no-repository"),
        ],
    )
    def test_falls_back_to_the_transcript(self, client, session_fixture, run_fixture, setup):
        """Anything the link cannot resolve to exactly one scoped request lands on the transcript:
        guessing would send the reader to a list excluding the request they came from."""
        setup(session_fixture, run_fixture)

        resp = self._get(client, session_fixture)

        # 302, never 301 — a browser would cache the pre-backfill fallback forever.
        assert resp.status_code == 302
        assert resp["Location"] == reverse("session_detail", kwargs={"thread_id": session_fixture.thread_id})

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
