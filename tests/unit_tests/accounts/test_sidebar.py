import uuid

from django.test import Client
from django.urls import reverse

import pytest
from notifications.models import Notification
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import Role, User
from tests.unit_tests.translation_stub import catalog


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
            ("session_list", lambda u: {}),
            ("schedule_list", lambda u: {}),
            ("sandbox_envs:list", lambda u: {}),
            ("user_channels", lambda u: {}),
            ("api_keys", lambda u: {}),
            ("mfa_list_webauthn", lambda u: {}),
            ("notifications:list", lambda u: {}),
        ],
    )
    def test_sidebar_present_on_every_section_root(self, member, url_name, kwargs_fn):
        response = _client(member).get(reverse(url_name, kwargs=kwargs_fn(member)))
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content
        assert b'data-testid="mobile-top-bar"' in response.content


@pytest.mark.django_db
class TestMobileTopBar:
    def test_opens_the_sidebar_sheet_and_shows_unread(self, member):
        content = _client(member).get(reverse("dashboard")).content.decode()
        bar = content.split('data-testid="mobile-top-bar"', 1)[1].split("</header>", 1)[0]
        assert "md:hidden" in bar.split(">", 1)[0]
        assert '@click="mobileNavOpen = !mobileNavOpen"' in bar
        assert 'x-show="$store.nav.unread > 0"' in bar


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


def _account_menu(content: str) -> str:
    """The account chip and its menu, sliced up to the sign-out form's `</form>`."""
    return content.split('data-testid="app-user-menu"', 1)[1].split("</form>", 1)[0]


@pytest.mark.django_db
class TestAccountMenu:
    def test_holds_personal_settings_and_sign_out(self, member):
        menu = _account_menu(_client(member).get(reverse("dashboard")).content.decode())
        for url_name in ("user_channels", "api_keys", "mfa_list_webauthn", "account_logout"):
            assert reverse(url_name) in menu

    @pytest.mark.parametrize("url_name", ["user_channels", "api_keys", "mfa_list_webauthn"])
    def test_chip_is_active_on_the_pages_it_holds(self, member, url_name):
        """Those pages have no nav item of their own, so the chip is the only element that can mark them active."""
        chip = _account_menu(_client(member).get(reverse(url_name)).content.decode()).split("</button>", 1)[0]
        assert "sidebar__nav-item--active" in chip

    def test_chip_is_inactive_elsewhere(self, member):
        chip = _account_menu(_client(member).get(reverse("dashboard")).content.decode()).split("</button>", 1)[0]
        assert "sidebar__nav-item--active" not in chip


@pytest.mark.django_db
class TestNotificationsNavItem:
    def test_sidebar_links_to_notifications(self, member):
        content = _client(member).get(reverse("dashboard")).content.decode()
        assert 'data-testid="nav-notifications"' in content
        assert reverse("notifications:list") in content

    def test_badge_is_bound_to_the_store(self, member):
        Notification.objects.create(
            recipient=member, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        content = _client(member).get(reverse("dashboard")).content.decode()
        assert 'data-testid="nav-unread-badge"' in content
        assert 'x-text="$store.nav.unread"' in content
        # Seeded server-side so the badge does not flash in before the stream connects.
        assert "unread_count: 1" in content


@pytest.mark.django_db
class TestArtifactsNavItem:
    def test_sidebar_shows_artifacts_link(self, member):
        response = _client(member).get(reverse("dashboard"))
        content = response.content.decode()
        assert reverse("artifact_list") in content
        assert "Artifacts" in content


@pytest.mark.django_db
class TestRunningJobsBadge:
    """The badge's text is Alpine-bound to the `nav` store, so what the server controls
    is the seed handed to `$store.nav.start(...)` and the store expressions on the badge
    — the count itself is covered in test_context_processors.py."""

    def test_badge_is_bound_to_the_store_not_server_rendered(self, member):
        response = _client(member).get(reverse("dashboard"))
        content = response.content.decode()
        # Present at any count (x-show hides it at zero), so a 0 → 1 transition has an
        # element to reveal without a page load.
        assert 'data-testid="nav-running-badge"' in content
        assert 'x-show="$store.nav.running > 0"' in content
        assert 'x-text="$store.nav.runningLabel"' in content

    def test_seeds_the_store_with_zero_when_nothing_is_running(self, member):
        response = _client(member).get(reverse("dashboard"))
        assert "running_runs: 0" in response.content.decode()

    def test_badge_shows_count_when_running(self, member):
        session1 = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api", user=member
        )
        session2 = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api2", user=member
        )
        Run.objects.create(
            session=session1,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.UI_JOB,
            repo_id="daiv/api",
            user=member,
        )
        Run.objects.create(
            session=session2,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.UI_JOB,
            repo_id="daiv/api2",
            user=member,
        )
        response = _client(member).get(reverse("dashboard"))
        content = response.content.decode()
        # The seed the store starts from, and the label template it interpolates into.
        assert "running_runs: 2" in content
        assert "{count} running" in content

    def test_the_label_reaches_the_page_translated(self, member):
        """Guards the placeholder style, not just the wiring: ``{% translate %}`` doubles
        every ``%`` before the catalog lookup, so a ``%(count)s`` msgid misses and renders
        the untranslated source — which reads as correct in the default locale."""
        with catalog({"{count} running": "{count} a decorrer"}):
            content = _client(member).get(reverse("dashboard")).content.decode()
        assert "{count} a decorrer" in content
        assert "{count} running" not in content


@pytest.mark.django_db
class TestNavActiveState:
    """Satisfies spec §5: for each section key, render a representative page and
    confirm the correct sidebar item carries the active CSS classes."""

    @pytest.mark.parametrize(
        "url_name,expected_section",
        [
            ("dashboard", "dashboard"),
            ("session_list", "sessions"),
            ("schedule_list", "schedules"),
            ("sandbox_envs:list", "sandbox_envs"),
            ("user_channels", "channels"),
            ("api_keys", "api_keys"),
            ("artifact_list", "artifacts"),
            ("notifications:list", "notifications"),
        ],
    )
    def test_active_section_matches_url(self, admin, url_name, expected_section):
        response = _client(admin).get(reverse(url_name))
        assert response.status_code == 200
        assert response.context["nav_active_section"] == expected_section

    def test_admin_only_sections_resolve_for_admin(self, admin):
        users_response = _client(admin).get(reverse("user_list"))
        assert users_response.context["nav_active_section"] == "users"
        config_response = _client(admin).get(reverse("site_configuration", kwargs={"group_key": "agent"}))
        assert config_response.context["nav_active_section"] == "configuration"
