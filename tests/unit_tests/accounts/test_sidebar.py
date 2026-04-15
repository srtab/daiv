from django.test import Client
from django.urls import reverse

import pytest
from activity.models import Activity, ActivityStatus, TriggerType

from accounts.models import Role, User


@pytest.fixture
def member(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


@pytest.fixture
def admin(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="x123456789",  # noqa: S106
        role=Role.ADMIN,
    )


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.mark.django_db
class TestSidebarSmoke:
    @pytest.mark.parametrize(
        "url_name,kwargs_fn",
        [
            ("dashboard", lambda u: {}),
            ("activity_list", lambda u: {}),
            ("schedule_list", lambda u: {}),
            ("user_channels", lambda u: {}),
            ("api_keys", lambda u: {}),
        ],
    )
    def test_sidebar_present_on_every_section_root(self, member, url_name, kwargs_fn):
        response = _client(member).get(reverse(url_name, kwargs=kwargs_fn(member)))
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content


@pytest.mark.django_db
class TestAdminGroupVisibility:
    def test_admin_sees_admin_group(self, admin):
        response = _client(admin).get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' in response.content
        assert b"Users" in response.content
        assert b"Configuration" in response.content

    def test_member_does_not_see_admin_group(self, member):
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' not in response.content


@pytest.mark.django_db
class TestRunningJobsBadge:
    def test_no_badge_when_zero_running(self, member):
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-running-badge"' not in response.content

    def test_badge_shows_count_when_running(self, member):
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=member, repo_id="daiv/api"
        )
        Activity.objects.create(
            status=ActivityStatus.RUNNING, trigger_type=TriggerType.MCP_JOB, user=member, repo_id="daiv/api"
        )
        response = _client(member).get(reverse("dashboard"))
        assert b'data-testid="nav-running-badge"' in response.content
        assert b"2 running" in response.content


@pytest.mark.django_db
class TestNavActiveState:
    """Satisfies spec §5: for each section key, render a representative page and
    confirm the correct sidebar item carries the active CSS classes."""

    @pytest.mark.parametrize(
        "url_name,expected_section",
        [
            ("dashboard", "dashboard"),
            ("activity_list", "activity"),
            ("schedule_list", "schedules"),
            ("user_channels", "channels"),
            ("api_keys", "api_keys"),
        ],
    )
    def test_active_section_matches_url(self, admin, url_name, expected_section):
        response = _client(admin).get(reverse(url_name))
        assert response.status_code == 200
        assert response.context["nav_active_section"] == expected_section

    def test_admin_only_sections_resolve_for_admin(self, admin):
        users_response = _client(admin).get(reverse("user_list"))
        assert users_response.context["nav_active_section"] == "users"
        config_response = _client(admin).get(reverse("site_configuration"))
        assert config_response.context["nav_active_section"] == "configuration"
