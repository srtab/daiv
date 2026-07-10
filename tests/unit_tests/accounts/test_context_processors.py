import uuid

from django.contrib.auth.models import AnonymousUser
from django.db import Error as DatabaseError
from django.test import Client, RequestFactory
from django.urls import reverse

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.context_processors import _resolve_active_section, nav, running_jobs_count
from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


@pytest.mark.django_db
class TestNavContextProcessor:
    def test_returns_empty_for_anonymous_user(self):
        request = RequestFactory().get("/dashboard/")
        request.user = AnonymousUser()
        assert nav(request) == {}

    def test_returns_zero_running_jobs_when_none(self, user):
        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 0
        assert out["nav_active_section"] == ""

    def test_counts_only_running_jobs_owned_by_user(self, user, db):
        from sessions.models import Session

        session = Session.objects.create(
            thread_id="test-thread-1", origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=user
        )
        Run.objects.create(
            session=session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=user
        )
        Run.objects.create(
            session=session,
            status=RunStatus.SUCCESSFUL,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=user,
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        session2 = Session.objects.create(
            thread_id="test-thread-2", origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=other
        )
        Run.objects.create(
            session=session2,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=other,
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1

    def test_resolves_active_section_from_url_name(self, db):
        user_obj = User.objects.create_user(username="charlie", email="c@test.com", password="x123456789")  # noqa: S106
        client = Client()
        client.force_login(user_obj)
        response = client.get(reverse("dashboard"))
        assert response.context["nav_active_section"] == "dashboard"

    def test_running_jobs_falls_back_to_zero_on_database_error(self, user, mocker):
        # A transient DB failure should log-and-degrade the badge rather than crash rendering.
        failing_qs = mocker.MagicMock()
        failing_qs.filter.return_value.count.side_effect = DatabaseError("connection lost")
        mocker.patch("sessions.models.RunManager.visible_to", return_value=failing_qs)
        request = RequestFactory().get("/dashboard/")
        request.user = user
        assert running_jobs_count(request, user) == 0

    def test_running_jobs_memoizes_on_request(self, user, db, mocker):
        # Second call on the same request must not re-query the database.
        request = RequestFactory().get("/dashboard/")
        request.user = user
        assert running_jobs_count(request, user) == 0

        spy = mocker.patch("sessions.models.RunManager.visible_to")
        assert running_jobs_count(request, user) == 0
        spy.assert_not_called()

    def test_counts_running_jobs_on_readable_repos(self, user, db):
        from django.utils import timezone

        from allauth.socialaccount.models import SocialAccount

        from codebase.base import RepoAccessLevel
        from codebase.models import RepositoryAccess

        SocialAccount.objects.create(user=user, provider="gitlab", uid="alice-1")
        RepositoryAccess.objects.create(
            provider="gitlab",
            uid="alice-1",
            username=user.username,
            repo_id="daiv/api",
            access_level=RepoAccessLevel.READ,
            synced_at=timezone.now(),
        )
        other = User.objects.create_user(username="bob", email="bob@test.com", password="x123456789")  # noqa: S106
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.MCP_JOB, user=other, repo_id="daiv/api"
        )
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.MCP_JOB,
            status=RunStatus.RUNNING,
            user=other,
            repo_id="daiv/api",
        )

        request = RequestFactory().get("/dashboard/")
        request.user = user
        request.resolver_match = None
        out = nav(request)
        assert out["nav_running_jobs"] == 1  # bob's running job, on a repo alice can read


class TestResolveActiveSection:
    def test_returns_empty_when_resolver_match_is_missing(self):
        request = RequestFactory().get("/")
        request.resolver_match = None
        assert _resolve_active_section(request) == ""

    def test_returns_empty_for_view_name_outside_known_sections(self):
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "account_login"})()
        assert _resolve_active_section(request) == ""

    def test_namespaced_view_name_disambiguates_colliding_url_names(self):
        # ``sandbox_envs:list`` and ``notifications:list`` share ``url_name == "list"``;
        # only the sandbox-envs namespace should resolve to the ``sandbox_envs`` section.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "sandbox_envs:list"})()
        assert _resolve_active_section(request) == "sandbox_envs"

        request.resolver_match = type("Match", (), {"view_name": "notifications:list"})()
        assert _resolve_active_section(request) == ""

    def test_namespaced_agent_run_new_highlights_sessions_section(self):
        # The "Start a run" page lives in the ``runs`` namespace, so its view_name is
        # prefixed. The sessions section must list the prefixed name to highlight it.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "runs:agent_run_new"})()
        assert _resolve_active_section(request) == "sessions"
