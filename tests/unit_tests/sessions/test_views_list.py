from __future__ import annotations

import uuid

from django.test import Client
from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@test.com",
        password="testpass123",  # noqa: S106
    )


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_session(**kwargs) -> Session:
    defaults = {
        "thread_id": str(uuid.uuid4()),
        "origin": SessionOrigin.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
    }
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _create_run(session: Session, **kwargs) -> Run:
    defaults = {
        "session": session,
        "trigger_type": SessionOrigin.SCHEDULE,
        "repo_id": session.repo_id,
        "status": RunStatus.SUCCESSFUL,
    }
    defaults.update(kwargs)
    return Run.objects.create(**defaults)


@pytest.mark.django_db
class TestSessionListView:
    def test_unauthenticated_redirects_to_login(self):
        response = Client().get(reverse("session_list"))
        assert response.status_code == 302
        assert "/accounts/login/" in response.url

    def test_authenticated_user_can_access(self, logged_in_client, user):
        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

    def test_owner_scoping_excludes_other_users_sessions(self, logged_in_client, user):
        """Owner scoping is applied before filters — another user's sessions are invisible."""
        mine = _create_session(user=user, repo_id="mine/repo")
        other_user = User.objects.create_user(
            username="bob",
            email="bob@test.com",
            password="testpass123",  # noqa: S106
        )
        theirs = _create_session(user=other_user, repo_id="mine/repo")

        response = logged_in_client.get(reverse("session_list"), {"repo": "mine/repo"})

        assert response.status_code == 200
        sessions = list(response.context["sessions"])
        session_pks = [s.pk for s in sessions]
        assert mine.pk in session_pks
        assert theirs.pk not in session_pks

    def test_filter_by_status_on_latest_run(self, logged_in_client, user):
        """?status=SUCCESSFUL only returns sessions whose latest run is SUCCESSFUL."""
        success_session = _create_session(user=user)
        _create_run(success_session, status=RunStatus.SUCCESSFUL)

        failed_session = _create_session(user=user)
        _create_run(failed_session, status=RunStatus.FAILED)

        response = logged_in_client.get(reverse("session_list"), {"status": RunStatus.SUCCESSFUL})

        assert response.status_code == 200
        sessions = list(response.context["sessions"])
        session_pks = [s.pk for s in sessions]
        assert success_session.pk in session_pks
        assert failed_session.pk not in session_pks

    def test_invalid_filter_drops_silently(self, logged_in_client, user):
        """?status=bogus shows full list (strict=False) and current_status is empty."""
        session = _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"), {"status": "bogus"})

        assert response.status_code == 200
        session_pks = [s.pk for s in response.context["sessions"]]
        assert session.pk in session_pks
        assert response.context["current_status"] == ""

    def test_context_includes_origins_and_statuses(self, logged_in_client, user):
        """Context must include origins and statuses choice lists."""
        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200
        assert "origins" in response.context
        assert "statuses" in response.context
        assert len(response.context["origins"]) > 0
        assert len(response.context["statuses"]) > 0

    def test_date_param_names_are_date_from_and_date_to(self, logged_in_client, user):
        """Lock in the URL param names; values round-trip to template context."""
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"date_from": "2020-01-01", "date_to": "2100-01-01"})
        assert response.status_code == 200
        assert response.context["current_from"] == "2020-01-01"
        assert response.context["current_to"] == "2100-01-01"

    def test_has_active_filters_false_with_no_params(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"))
        assert response.context["has_active_filters"] is False
        assert response.context["current_batch_short"] == ""

    def test_has_active_filters_true_when_batch_is_set(self, logged_in_client, user):
        batch_id = uuid.uuid4()
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"batch": str(batch_id)})
        assert response.context["has_active_filters"] is True
        assert response.context["current_batch_short"] == str(batch_id)[:8]

    def test_in_flight_ids_contains_non_terminal_run_ids(self, logged_in_client, user):
        """in_flight_ids is a comma-joined string of non-terminal run PKs from page sessions."""
        session = _create_session(user=user)
        running_run = _create_run(session, status=RunStatus.RUNNING)
        _create_run(session, status=RunStatus.SUCCESSFUL)  # terminal — excluded

        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200

        in_flight_ids = response.context["in_flight_ids"]
        # The running run should be in the comma-joined string.
        assert str(running_run.pk) in in_flight_ids

    def test_in_flight_ids_excludes_terminal_runs(self, logged_in_client, user):
        """Terminal runs (SUCCESSFUL, FAILED) must not appear in in_flight_ids."""
        session = _create_session(user=user)
        successful_run = _create_run(session, status=RunStatus.SUCCESSFUL)
        failed_run = _create_run(session, status=RunStatus.FAILED)

        response = logged_in_client.get(reverse("session_list"))
        in_flight_ids = response.context["in_flight_ids"]

        assert str(successful_run.pk) not in in_flight_ids
        assert str(failed_run.pk) not in in_flight_ids

    def test_session_row_links_to_detail(self, logged_in_client, user):
        """Each row renders a stretched anchor to session_detail so the whole row is clickable."""
        session = _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"))

        assert response.status_code == 200
        expected_href = reverse("session_detail", kwargs={"thread_id": session.thread_id})
        assert f'href="{expected_href}"' in response.content.decode()

    def test_pagination_uses_paginate_by(self, logged_in_client, user):
        """Check that paginated results are returned when there are more than paginate_by sessions."""
        # Create 30 sessions to exceed typical paginate_by=25.
        for _ in range(30):
            _create_session(user=user)

        response = logged_in_client.get(reverse("session_list"))
        assert response.status_code == 200
        # Page 1 should have at most 25 sessions (default paginate_by).
        assert len(response.context["sessions"]) <= 25

        # Page 2 should exist.
        response_p2 = logged_in_client.get(reverse("session_list"), {"page": "2"})
        assert response_p2.status_code == 200
        assert len(response_p2.context["sessions"]) > 0

    def test_context_has_search_and_range(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"q": "hello", "range": "7d"})
        assert response.status_code == 200
        assert response.context["current_q"] == "hello"
        assert response.context["current_range"] == "7d"
        assert response.context["current_range_label"] == "Last 7 days"
        assert response.context["has_active_filters"] is True

    def test_has_active_filters_true_when_q_set(self, logged_in_client, user):
        _create_session(user=user)
        response = logged_in_client.get(reverse("session_list"), {"q": "abc"})
        assert response.context["has_active_filters"] is True

    def test_runs_are_prefetched_newest_first(self, logged_in_client, user, django_assert_num_queries):
        session = _create_session(user=user)
        _create_run(session, status=RunStatus.SUCCESSFUL)
        newest = _create_run(session, status=RunStatus.RUNNING)
        response = logged_in_client.get(reverse("session_list"))
        row = next(s for s in response.context["sessions"] if s.pk == session.pk)
        with django_assert_num_queries(0):
            latest = list(row.runs.all())[0]  # served from the prefetch cache — no query
        assert latest.pk == newest.pk
