import contextlib
import gettext as gettext_module
import uuid

from django.test import Client
from django.urls import reverse
from django.utils import translation
from django.utils.translation import trans_real

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin

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


class _StubCatalog(gettext_module.NullTranslations):
    def __init__(self, entries):
        super().__init__()
        self._entries = entries

    def gettext(self, message):
        return self._entries.get(message, message)


@contextlib.contextmanager
def _catalog(entries):
    """Activate a fake locale carrying ``entries``.

    A stub rather than a real locale because ``.mo`` files are gitignored and CI runs
    ``make test`` without ``compilemessages``, so no compiled catalog exists there.
    """
    trans_real._translations["xx"] = _StubCatalog(entries)
    try:
        with translation.override("xx"):
            yield
    finally:
        trans_real._translations.pop("xx", None)


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
        with _catalog({"{count} running": "{count} a decorrer"}):
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
