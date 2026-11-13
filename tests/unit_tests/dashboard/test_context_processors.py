import uuid
from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.db import Error as DatabaseError
from django.test import RequestFactory
from django.urls import reverse

import pytest
from dashboard.context_processors import _resolve_active_section, nav, running_jobs_count, sidebar_state
from dashboard.sidebar import SidebarLists
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


def _nav_for(user) -> dict:
    request = RequestFactory().get("/dashboard/")
    request.user = user
    request.resolver_match = None
    return nav(request)


@pytest.mark.django_db
class TestNavContextProcessor:
    def test_returns_empty_for_anonymous_user(self):
        assert _nav_for(AnonymousUser()) == {}

    def test_returns_zero_running_jobs_when_none(self, member_user):
        out = _nav_for(member_user)
        assert out["nav_running_jobs"] == 0
        assert out["nav_active_section"] == ""

    def test_counts_only_running_jobs_owned_by_user(self, member_user):
        session = Session.objects.create(
            thread_id="test-thread-1", origin=SessionOrigin.MCP_JOB, repo_id="daiv/api", user=member_user
        )
        Run.objects.create(
            session=session,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=member_user,
        )
        Run.objects.create(
            session=session,
            status=RunStatus.SUCCESSFUL,
            trigger_type=SessionOrigin.MCP_JOB,
            repo_id="daiv/api",
            user=member_user,
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

        assert _nav_for(member_user)["nav_running_jobs"] == 1
        assert _nav_for(member_user)["nav_own_running_jobs"] == 1

    def test_resolves_active_section_from_url_name(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert response.context["nav_active_section"] == "dashboard"

    def test_running_jobs_falls_back_to_zero_on_database_error(self, member_user, mocker):
        # A transient DB failure should log-and-degrade the badge rather than crash rendering.
        failing_qs = mocker.MagicMock()
        failing_qs.filter.return_value.aggregate.side_effect = DatabaseError("connection lost")
        mocker.patch("sessions.models.RunManager.visible_to", return_value=failing_qs)
        request = RequestFactory().get("/dashboard/")
        request.user = member_user
        assert running_jobs_count(request, member_user) == 0

    def test_running_jobs_memoizes_on_request(self, member_user, mocker):
        # Second call on the same request must not re-query the database.
        request = RequestFactory().get("/dashboard/")
        request.user = member_user
        assert running_jobs_count(request, member_user) == 0

        spy = mocker.patch("sessions.models.RunManager.visible_to")
        assert running_jobs_count(request, member_user) == 0
        spy.assert_not_called()

    def test_counts_running_jobs_on_readable_repos(self, member_user):
        from django.utils import timezone

        from allauth.socialaccount.models import SocialAccount

        from codebase.base import RepoAccessLevel
        from codebase.models import RepositoryAccess

        SocialAccount.objects.create(user=member_user, provider="gitlab", uid="alice-1")
        RepositoryAccess.objects.create(
            provider="gitlab",
            uid="alice-1",
            username=member_user.username,
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

        nav_context = _nav_for(member_user)
        assert nav_context["nav_running_jobs"] == 1  # bob's running job, on a repo the member can read
        assert nav_context["nav_own_running_jobs"] == 0


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
        # each namespace must resolve to its own section.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "sandbox_envs:list"})()
        assert _resolve_active_section(request) == "sandbox_envs"

        request.resolver_match = type("Match", (), {"view_name": "notifications:list"})()
        assert _resolve_active_section(request) == "notifications"

    def test_namespaced_agent_run_new_highlights_sessions_section(self):
        # The "Start a run" page lives in the ``runs`` namespace, so its view_name is
        # prefixed. The sessions section must list the prefixed name to highlight it.
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "runs:agent_run_new"})()
        assert _resolve_active_section(request) == "sessions"

    def test_nav_view_name_wins_over_resolved_url(self):
        request = RequestFactory().get("/dashboard/mcp-servers/1/edit/")
        request.resolver_match = type("Match", (), {"view_name": "mcp_connectors:edit"})()
        request.nav_view_name = "mcp_connectors:global_list"
        assert _resolve_active_section(request) == "mcp_servers_global"

    def test_cross_project_access_log_highlights_its_own_section(self):
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": "codebase:cross-project-access"})()
        assert _resolve_active_section(request) == "cross_project_access"

    @pytest.mark.parametrize("view_name", ["artifact_list", "session_artifact_detail", "session_artifact_raw"])
    def test_artifacts_section_covers_list_detail_and_raw(self, view_name):
        request = RequestFactory().get("/")
        request.resolver_match = type("Match", (), {"view_name": view_name})()
        assert _resolve_active_section(request) == "artifacts"


def _request_at(user, view_name: str = "", **kwargs):
    request = RequestFactory().get("/")
    request.user = user
    request.resolver_match = SimpleNamespace(view_name=view_name, kwargs=kwargs)
    return request


@pytest.mark.django_db
class TestNavSidebar:
    @pytest.mark.parametrize(
        ("view_name", "settings_mode"),
        [
            ("sandbox_envs:list", True),
            ("mcp_connectors:global_list", True),
            ("schedule_template_list", True),
            ("schedule_list", False),
            ("insights", False),
            ("session_list", False),
            ("user_channels", False),
        ],
    )
    def test_settings_mode_is_on_for_settings_sections(self, member_user, view_name, settings_mode):
        assert nav(_request_at(member_user, view_name))["nav_settings_mode"] is settings_mode

    def test_sidebar_state_is_computed_once_per_request(self, member_user, django_assert_num_queries):
        Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="a/b", user=member_user)
        request = _request_at(member_user, "dashboard")
        context = nav(request)
        assert len(context["nav_sidebar"].sessions) == 1

        with django_assert_num_queries(0):
            assert len(context["nav_sidebar"].sessions) == 1
            assert len(nav(request)["nav_sidebar"].sessions) == 1

    def test_sidebar_is_not_queried_when_unread(self, member_user, django_assert_num_queries):
        with django_assert_num_queries(0):
            nav(_request_at(member_user, "dashboard"))

    def test_sidebar_is_flagged_failed_on_database_error(self, member_user, mocker):
        sidebar = mocker.patch("dashboard.context_processors.Sidebar")
        sidebar.return_value.lists.side_effect = DatabaseError("connection lost")

        assert sidebar_state(_request_at(member_user, "dashboard"), member_user) == SidebarLists(failed=True)

    @pytest.mark.parametrize(
        ("view_name", "viewing"), [("session_detail", "thread-1"), ("session_artifact_detail", ""), ("dashboard", "")]
    )
    def test_viewing_comes_from_the_session_detail_url(self, member_user, mocker, view_name, viewing):
        spy = mocker.patch("dashboard.context_processors.Sidebar")
        spy.return_value.lists.return_value = SidebarLists()
        request = _request_at(member_user, view_name, thread_id="thread-1")

        sidebar_state(request, member_user)

        spy.assert_called_once_with(member_user, viewing=viewing)
        assert nav(request)["nav_session_thread_id"] == viewing
