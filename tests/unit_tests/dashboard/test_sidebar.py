import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest
from bs4 import BeautifulSoup
from dashboard import context_processors
from dashboard.sidebar import SESSIONS_PAGE
from notifications.models import Notification
from sessions.hydration import HydratedThread
from sessions.models import Run, RunStatus, Session, SessionOrigin

from schedules.models import ScheduledJob
from tests.unit_tests.dashboard.factories import make_firing, make_run, make_schedule, make_session
from tests.unit_tests.translation_stub import catalog


@pytest.mark.django_db
class TestSidebarSmoke:
    @pytest.mark.parametrize(
        "url_name",
        [
            "dashboard",
            "session_list",
            "schedule_list",
            "sandbox_envs:list",
            "user_channels",
            "api_keys",
            "mfa_list_webauthn",
            "notifications:list",
        ],
    )
    def test_sidebar_present_on_every_section_root(self, member_client, url_name):
        response = member_client.get(reverse(url_name))
        assert response.status_code == 200
        assert b'data-testid="app-sidebar"' in response.content
        assert b'data-testid="app-user-menu"' in response.content
        assert b'data-testid="mobile-top-bar"' in response.content


@pytest.mark.django_db
class TestSidebarDocsLink:
    def test_footer_links_the_user_docs_with_a_collapsible_label(self, member_client):
        content = member_client.get(reverse("sandbox_envs:list")).content.decode()
        link = BeautifulSoup(content, "html.parser").select_one('[data-testid="sidebar-docs-link"]')
        assert link["href"] == "https://srtab.github.io/daiv/latest/"
        assert link["target"] == "_blank"
        assert "sidebar__footer-link" in link["class"]
        assert link.select_one(".sidebar__collapsible").get_text() == "Help & docs"

    def test_label_is_translated(self, member_client):
        with catalog({"Help & docs": "Ajuda"}):
            content = member_client.get(reverse("sandbox_envs:list")).content.decode()
        assert '<span class="sidebar__collapsible">Ajuda</span>' in content


@pytest.mark.django_db
class TestMobileTopBar:
    def test_opens_the_sidebar_sheet_and_shows_unread(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        bar = content.split('data-testid="mobile-top-bar"', 1)[1].split("</header>", 1)[0]
        assert "md:hidden" in bar.split(">", 1)[0]
        assert '@click="mobileNavOpen = !mobileNavOpen"' in bar
        assert 'x-show="$store.nav.unread > 0"' in bar


def _account_menu(content: str) -> str:
    """The account chip and its menu, sliced up to the sign-out form's `</form>`."""
    return content.split('data-testid="app-user-menu"', 1)[1].split("</form>", 1)[0]


@pytest.mark.django_db
class TestAccountMenu:
    def test_holds_personal_settings_and_sign_out(self, member_client):
        menu = _account_menu(member_client.get(reverse("dashboard")).content.decode())
        for url_name in ("user_channels", "api_keys", "mfa_list_webauthn", "account_logout"):
            assert reverse(url_name) in menu

    @pytest.mark.parametrize("url_name", ["user_channels", "api_keys", "mfa_list_webauthn"])
    def test_chip_is_active_on_the_pages_it_holds(self, member_client, url_name):
        """Those pages have no nav item of their own, so the chip is the only element that can mark them active."""
        chip = _account_menu(member_client.get(reverse(url_name)).content.decode()).split("</button>", 1)[0]
        assert "sidebar__nav-item--active" in chip

    def test_chip_is_inactive_elsewhere(self, member_client):
        chip = _account_menu(member_client.get(reverse("dashboard")).content.decode()).split("</button>", 1)[0]
        assert "sidebar__nav-item--active" not in chip


@pytest.mark.django_db
class TestNotificationsNavItem:
    def test_sidebar_links_to_notifications(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'data-testid="nav-notifications"' in content
        assert reverse("notifications:list") in content

    def test_badge_is_bound_to_the_store(self, member_user, member_client):
        Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="n", body="b", link_url="/"
        )
        content = member_client.get(reverse("dashboard")).content.decode()
        assert 'data-testid="nav-unread-badge"' in content
        assert 'x-text="$store.nav.unread"' in content
        # Seeded server-side so the badge does not flash in before the stream connects.
        assert "unread_count: 1" in content


@pytest.mark.django_db
class TestRunningJobsBadge:
    """The badge's text is Alpine-bound to the `nav` store, so what the server controls
    is the seed handed to `$store.nav.start(...)` and the store expressions on the badge
    — the count itself is covered in test_context_processors.py."""

    def test_seeds_the_store_with_zero_when_nothing_is_running(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert "running_runs: 0" in response.content.decode()

    def test_badge_shows_count_when_running(self, member_user, member_client):
        session1 = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api", user=member_user
        )
        session2 = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api2", user=member_user
        )
        Run.objects.create(
            session=session1,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.UI_JOB,
            repo_id="daiv/api",
            user=member_user,
        )
        Run.objects.create(
            session=session2,
            status=RunStatus.RUNNING,
            trigger_type=SessionOrigin.UI_JOB,
            repo_id="daiv/api2",
            user=member_user,
        )
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        # The seed the store starts from, and the label template it interpolates into.
        assert "running_runs: 2" in content
        assert "{count} running" in content

    def test_the_label_reaches_the_page_translated(self, member_client):
        """Guards the placeholder style, not just the wiring: ``{% translate %}`` doubles
        every ``%`` before the catalog lookup, so a ``%(count)s`` msgid misses and renders
        the untranslated source — which reads as correct in the default locale."""
        with catalog({"{count} running": "{count} a decorrer"}):
            content = member_client.get(reverse("dashboard")).content.decode()
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
            ("insights", "insights"),
            ("session_list", "sessions"),
            ("schedule_list", "schedules"),
            ("sandbox_envs:list", "sandbox_envs"),
            ("user_channels", "account"),
            ("api_keys", "account"),
            ("mfa_list_webauthn", "account"),
            ("artifact_list", "artifacts"),
            ("notifications:list", "notifications"),
        ],
    )
    def test_active_section_matches_url(self, admin_client, url_name, expected_section):
        response = admin_client.get(reverse(url_name))
        assert response.status_code == 200
        assert response.context["nav_active_section"] == expected_section

    def test_admin_only_sections_resolve_for_admin(self, admin_client):
        users_response = admin_client.get(reverse("user_list"))
        assert users_response.context["nav_active_section"] == "users"
        config_response = admin_client.get(reverse("site_configuration", kwargs={"group_key": "agent"}))
        assert config_response.context["nav_active_section"] == "configuration"


def _soup(client, url_name="session_list", **kwargs):
    response = client.get(reverse(url_name, kwargs=kwargs or None), follow=True)
    return BeautifulSoup(response.content.decode(), "html.parser")


def _sidebar(client, url_name="dashboard", **kwargs):
    """The persistent sidebar; the mobile sheet renders a second copy of the partial."""
    return _soup(client, url_name, **kwargs).select_one("#app-sidebar")


def _testids(sidebar, wanted) -> list[str]:
    return [node["data-testid"] for node in sidebar.select("[data-testid]") if node["data-testid"] in wanted]


def _session_page(client, session):
    with patch(
        "sessions.page_state.ahydrate_thread", AsyncMock(return_value=HydratedThread([], False, None, None, None))
    ):
        return _sidebar(client, "session_detail", thread_id=session.thread_id)


NORMAL_ORDER = ["nav-search", "nav-home", "nav-notifications", "nav-artifacts", "sidebar-lists", "nav-settings"]


@pytest.mark.django_db
class TestSidebarOrder:
    def test_normal_mode_order(self, member_client):
        assert _testids(_sidebar(member_client), NORMAL_ORDER) == NORMAL_ORDER

    def test_home_links_the_dashboard(self, member_client):
        assert _sidebar(member_client).select_one('[data-testid="nav-home"]')["href"] == reverse("dashboard")

    def test_pages_that_left_the_top_level_are_gone(self, admin_client):
        hrefs = {link["href"] for link in _sidebar(admin_client).select("a[href]")}
        for url_name in ("insights", "user_list", "skills:list", "memory:list", "schedule_template_list"):
            assert reverse(url_name) not in hrefs

    def test_mobile_sheet_shows_the_same_rows(self, member_client):
        sheet = _soup(member_client, "dashboard").select('[data-testid="app-sidebar"]')[1]
        assert _testids(sheet, NORMAL_ORDER) == NORMAL_ORDER

    def test_settings_row_sits_outside_the_scrolling_lists(self, member_client):
        sidebar = _sidebar(member_client)
        settings = sidebar.select_one('[data-testid="nav-settings"]')
        assert settings["href"] == reverse("sandbox_envs:list")
        assert settings["title"]
        assert settings.find_parent(attrs={"data-sidebar-scroll": True}) is None
        assert sidebar.select_one('[data-testid="sidebar-lists"]').find_parent(attrs={"data-sidebar-scroll": True})


def _group(sidebar, testid):
    return sidebar.select_one(f'[data-testid="{testid}"]')


@pytest.mark.django_db
class TestSidebarSessionsGroup:
    def test_rows_link_their_sessions(self, member_user, member_client):
        session = make_session(member_user, title="Fix the login bug")

        (row,) = _sidebar(member_client).select('[data-testid="nav-session-row"]')

        assert row["href"] == reverse("session_detail", kwargs={"thread_id": session.thread_id})
        assert row["title"] == "Fix the login bug"
        assert row["data-row-key"] == f"s:{session.thread_id}"
        assert "Fix the login bug" in row.get_text()

    def test_untitled_session_says_generating_title(self, member_user, member_client):
        make_session(member_user, title="")

        (row,) = _sidebar(member_client).select('[data-testid="nav-session-row"]')

        assert "generating title…" in row.get_text()

    def test_running_session_shows_its_state_word(self, member_user, member_client):
        make_run(make_session(member_user), RunStatus.RUNNING)

        (row,) = _sidebar(member_client).select('[data-testid="nav-session-row"]')

        assert "Running" in row.get_text()
        assert row.select_one(".status-dot-running")

    def test_finished_session_shows_a_relative_time(self, member_user, member_client):
        make_run(make_session(member_user, minutes_ago=5), RunStatus.SUCCESSFUL)

        (row,) = _sidebar(member_client).select('[data-testid="nav-session-row"]')

        assert "Done" not in row.get_text()
        assert "5m" in row.get_text()

    def test_viewed_session_row_is_active(self, member_user, member_client):
        viewed = make_session(member_user, minutes_ago=5)
        make_session(member_user)

        rows = _session_page(member_client, viewed).select('[data-testid="nav-session-row"]')

        assert ["nav-row--active" in row["class"] for row in rows] == [False, True]
        assert rows[1]["aria-current"] == "page"

    def test_show_more_only_when_there_is_more(self, member_user, member_client):
        for minutes_ago in range(SESSIONS_PAGE):
            make_session(member_user, minutes_ago=minutes_ago)
        assert not _sidebar(member_client).select('[data-testid="nav-sessions-more"]')

        make_session(member_user, minutes_ago=99)
        (more,) = _sidebar(member_client).select('[data-testid="nav-sessions-more"]')
        assert more["href"] == reverse("session_list")

    def test_empty_state_invites_a_chat(self, member_client):
        group = _group(_sidebar(member_client), "nav-sessions-group")

        assert not group.select('[data-testid="nav-session-row"]')
        assert group.select_one(f'a[href="{reverse("session_new_chat")}"]')

    def test_all_links_the_sessions_page(self, member_client):
        assert _sidebar(member_client).select_one('[data-testid="nav-sessions-all"]')["href"] == reverse("session_list")

    def test_lists_container_names_its_endpoint_and_viewed_session(self, member_user, member_client):
        session = make_session(member_user)

        on_home = _sidebar(member_client).select_one('[data-testid="sidebar-lists"]')
        on_session = _session_page(member_client, session).select_one('[data-testid="sidebar-lists"]')

        assert on_home["data-url"] == reverse("sidebar_lists")
        assert on_home["data-viewing"] == ""
        assert on_session["data-viewing"] == session.thread_id


@pytest.mark.django_db
class TestSidebarScheduledGroup:
    def test_schedule_row_shows_name_and_next_run(self, member_user, member_client):
        schedule = make_schedule(member_user, name="Weekly dependency audit")
        ScheduledJob.objects.filter(pk=schedule.pk).update(next_run_at=timezone.now() + timedelta(hours=3, minutes=30))

        (row,) = _sidebar(member_client).select('[data-testid="nav-schedule-row"]')

        assert row["data-schedule"] == str(schedule.pk)
        assert row["data-starts-open"] == "false"
        assert "Weekly dependency audit" in row.get_text()
        assert "in 3h" in row.get_text()

    def test_failed_schedule_starts_open_with_its_state(self, member_user, member_client):
        make_firing(make_schedule(member_user), RunStatus.FAILED)

        (row,) = _sidebar(member_client).select('[data-testid="nav-schedule-row"]')
        (firing,) = row.select('[data-testid="nav-firing-row"]')

        assert row["data-starts-open"] == "true"
        assert "Failed" in row.select_one("[data-row-key]").get_text()
        assert firing.find_parent(attrs={"hidden": True}) is None

    def test_closed_schedule_keeps_its_firings_hidden(self, member_user, member_client):
        make_firing(make_schedule(member_user), RunStatus.SUCCESSFUL)

        (firing,) = _sidebar(member_client).select('[data-testid="nav-firing-row"]')

        assert firing.find_parent(attrs={"hidden": True})

    def test_multi_repo_firing_shows_the_count_and_links_the_batch(self, member_user, member_client):
        batch_id, _ = make_firing(make_schedule(member_user), RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL)

        (firing,) = _sidebar(member_client).select('[data-testid="nav-firing-row"]')

        assert firing["href"] == reverse("session_list") + f"?batch={batch_id}"
        assert "2 repos" in firing.get_text()
        assert "Done" in firing.get_text()

    def test_all_runs_links_the_schedule_filter(self, member_user, member_client):
        schedule = make_schedule(member_user)

        (all_runs,) = _sidebar(member_client).select('[data-testid="nav-schedule-all-runs"]')

        assert all_runs["href"] == reverse("session_list") + f"?schedule={schedule.pk}"

    def test_manage_links_schedules_and_heading_is_active_there(self, member_client):
        on_home = _group(_sidebar(member_client), "nav-scheduled-group")
        on_schedules = _group(_sidebar(member_client, "schedule_list"), "nav-scheduled-group")

        assert on_home.select_one('[data-testid="nav-scheduled-manage"]')["href"] == reverse("schedule_list")
        assert not on_home.select(".sidebar__nav-item--active")
        assert on_schedules.select(".sidebar__nav-item--active")

    def test_empty_state_links_schedule_create(self, member_client):
        group = _group(_sidebar(member_client), "nav-scheduled-group")

        assert not group.select('[data-testid="nav-schedule-row"]')
        assert group.select_one(f'a[href="{reverse("schedule_create")}"]')


@pytest.mark.django_db
class TestSidebarRail:
    def test_lists_are_collapsible_and_rail_icons_are_rail_only(self, member_client):
        sidebar = _sidebar(member_client)

        assert sidebar.select_one('[data-testid="sidebar-lists"]').find_parent(class_="sidebar__collapsible")
        sessions = sidebar.select_one('[data-testid="nav-rail-sessions"]')
        scheduled = sidebar.select_one('[data-testid="nav-rail-scheduled"]')
        assert sessions.find_parent(class_="sidebar__rail-only")
        assert scheduled.find_parent(class_="sidebar__rail-only")
        assert sessions["href"] == reverse("session_list")
        assert scheduled["href"] == reverse("schedule_list")
        assert sessions.select_one('[x-show="$store.nav.running > 0"]')


@pytest.mark.django_db
class TestSidebarQueries:
    def test_both_copies_share_one_computation(self, member_client, mocker):
        spy = mocker.spy(context_processors, "sidebar_lists")

        member_client.get(reverse("dashboard"))

        assert spy.call_count == 1

    def test_query_count_does_not_grow_with_rows(self, member_user, member_client):
        for minutes_ago in range(3):
            make_session(member_user, minutes_ago=minutes_ago)
        member_client.get(reverse("artifact_list"))
        with CaptureQueriesContext(connection) as few:
            member_client.get(reverse("artifact_list"))

        for minutes_ago in range(3, 12):
            make_session(member_user, minutes_ago=minutes_ago)
        with CaptureQueriesContext(connection) as many:
            member_client.get(reverse("artifact_list"))

        assert len(many) == len(few)


def _item_label(item) -> str:
    label = item.select_one(".sidebar__collapsible")
    return (label.find("span") or label).get_text(strip=True)


MEMBER_SETTINGS = ["sandbox_envs:list", "mcp_connectors:list", "memory:list"]
ADMIN_SETTINGS = [
    "user_list",
    "site_configuration_index",
    "schedule_template_list",
    "skills:list",
    "mcp_connectors:global_list",
]
NORMAL_PAGES = [
    "dashboard",
    "session_list",
    "schedule_list",
    "insights",
    "artifact_list",
    "notifications:list",
    "user_channels",
]
SETTINGS_ORDER = [
    "nav-settings-back",
    "nav-setup-group",
    "nav-admin-group",
    "sidebar-docs-link",
    "build-info",
    "nav-settings",
]


@pytest.mark.django_db
class TestSidebarSettingsMode:
    @pytest.mark.parametrize("url_name", MEMBER_SETTINGS)
    def test_member_settings_pages_show_the_settings_menu(self, member_client, url_name):
        sidebar = _sidebar(member_client, url_name)

        assert sidebar.select_one('[data-testid="nav-settings-back"]')
        assert not sidebar.select('[data-testid="sidebar-lists"]')
        assert not sidebar.select('[data-testid="nav-search"]')

    @pytest.mark.parametrize("url_name", ADMIN_SETTINGS)
    def test_admin_settings_pages_show_the_settings_menu(self, admin_client, url_name):
        sidebar = _sidebar(admin_client, url_name)

        assert sidebar.select_one('[data-testid="nav-settings-back"]')
        assert not sidebar.select('[data-testid="sidebar-lists"]')

    @pytest.mark.parametrize("url_name", NORMAL_PAGES)
    def test_other_pages_show_the_normal_sidebar(self, member_client, url_name):
        sidebar = _sidebar(member_client, url_name)

        assert sidebar.select_one('[data-testid="sidebar-lists"]')
        assert not sidebar.select('[data-testid="nav-settings-back"]')

    def test_menu_order(self, admin_client):
        assert _testids(_sidebar(admin_client, "sandbox_envs:list"), SETTINGS_ORDER) == SETTINGS_ORDER

    def test_mobile_sheet_shows_the_same_menu(self, admin_client):
        sheet = _soup(admin_client, "sandbox_envs:list").select('[data-testid="app-sidebar"]')[1]

        assert _testids(sheet, SETTINGS_ORDER) == SETTINGS_ORDER

    def test_member_does_not_see_admin_group(self, member_client):
        assert not _sidebar(member_client, "sandbox_envs:list").select('[data-testid="nav-admin-group"]')

    def test_admin_group_lists_its_five_pages(self, admin_client):
        group = _sidebar(admin_client, "sandbox_envs:list").select_one('[data-testid="nav-admin-group"]')

        assert [link["href"] for link in group.select("a.sidebar__nav-item")] == [
            reverse(url_name) for url_name in ADMIN_SETTINGS
        ]

    @pytest.mark.parametrize(
        ("url_name", "label"),
        [
            ("sandbox_envs:list", "Sandbox environments"),
            ("mcp_connectors:list", "MCP servers"),
            ("memory:list", "Memory"),
            ("user_list", "Users"),
            ("site_configuration_index", "Configuration"),
            ("schedule_template_list", "Schedule templates"),
            ("skills:list", "Skills"),
            ("mcp_connectors:global_list", "Global MCP servers"),
        ],
    )
    def test_the_active_item_is_the_page_section(self, admin_client, url_name, label):
        menu = _sidebar(admin_client, url_name).select_one('[data-testid="nav-settings-menu"]')

        assert [_item_label(item) for item in menu.select(".sidebar__nav-item--active")] == [label]

    def test_settings_row_is_active_only_in_settings_mode(self, member_client):
        def settings_row(url_name):
            return _sidebar(member_client, url_name).select_one('[data-testid="nav-settings"]')

        assert "sidebar__nav-item--active" in settings_row("sandbox_envs:list")["class"]
        assert "sidebar__nav-item--active" not in settings_row("dashboard")["class"]

    def test_agent_setup_items_say_what_they_are_for(self, member_client):
        group = _sidebar(member_client, "sandbox_envs:list").select_one('[data-testid="nav-setup-group"]')
        described = group.select(".sidebar__nav-item--described")

        assert [_item_label(item) for item in described] == ["Sandbox environments", "MCP servers", "Memory"]
        for item in described:
            description = item.select_one('[data-testid="nav-description"]').get_text(strip=True)
            assert description
            # The rail hides the description, so the tooltip has to carry it.
            assert item["title"] == f"{_item_label(item)}: {description}"

    def test_agent_setup_heading_says_it_is_optional(self, member_client):
        group = _sidebar(member_client, "sandbox_envs:list").select_one('[data-testid="nav-setup-group"]')
        heading = group.select_one(".sidebar__group-heading")

        assert "Optional" in heading.get_text()
        assert heading["title"].startswith("Optional")

    def test_docs_api_and_build_line_live_here(self, member_client):
        settings = _sidebar(member_client, "sandbox_envs:list")
        home = _sidebar(member_client, "dashboard")
        api_docs = f'a[href="{reverse("api:openapi-view")}"]'

        for selector in ('[data-testid="sidebar-docs-link"]', '[data-testid="build-info"]', api_docs):
            assert settings.select_one(selector)
            assert not home.select_one(selector)

    def test_back_goes_home_and_new_and_account_stay(self, member_client):
        sidebar = _sidebar(member_client, "sandbox_envs:list")

        back = sidebar.select_one('[data-testid="nav-settings-back"]')
        assert back["href"] == reverse("dashboard")
        assert back["title"]
        assert sidebar.select_one('[data-testid="nav-new"]')
        assert sidebar.select_one('[data-testid="app-user-menu"]')

    def test_settings_pages_run_no_sidebar_list_queries(self, member_client, mocker):
        spy = mocker.spy(context_processors, "sidebar_lists")

        member_client.get(reverse("sandbox_envs:list"))

        assert spy.call_count == 0

    def test_every_page_stays_reachable(self, admin_client):
        home = _soup(admin_client, "dashboard")
        reachable = {
            link["href"]
            for scope in (home.select_one("#app-sidebar"), home.select_one("main"), _sidebar(admin_client, "user_list"))
            for link in scope.select("a[href]")
        }
        pages = ["insights", "schedule_list", "artifact_list", "session_list", *MEMBER_SETTINGS, *ADMIN_SETTINGS]

        assert [url_name for url_name in pages if reverse(url_name) not in reachable] == []


@pytest.mark.django_db
class TestHome:
    def test_links_artifacts(self, member_client):
        main = _soup(member_client, "dashboard").select_one("main")

        assert main.select_one(f'a[href="{reverse("artifact_list")}"]')


@pytest.mark.django_db
class TestNewSplitButton:
    @pytest.mark.parametrize("testid", ["nav-new", "topbar-new"])
    def test_main_part_starts_a_chat(self, member_client, testid):
        split = _soup(member_client).select_one(f'[data-testid="{testid}"]')
        assert split.select_one(f'[data-testid="{testid}-chat"]')["href"] == reverse("session_new_chat")

    @pytest.mark.parametrize("testid", ["nav-new", "topbar-new"])
    def test_menu_offers_chat_and_background_run(self, member_client, testid):
        split = _soup(member_client).select_one(f'[data-testid="{testid}"]')
        items = split.select('[role="menu"] [role="menuitem"]')
        assert [(a["href"], a.select_one("span span").get_text(strip=True)) for a in items] == [
            (reverse("session_new_chat"), "Chat"),
            (reverse("runs:agent_run_new"), "Background run"),
        ]
        # Items are reached with the arrow keys, not Tab (menu button pattern).
        assert all(a["tabindex"] == "-1" for a in items)

    def test_toggle_is_a_keyboard_menu_button(self, member_client):
        split = _soup(member_client).select_one('[data-testid="nav-new"]')
        toggle = split.select_one('[data-testid="nav-new-toggle"]')
        assert toggle.name == "button"
        assert toggle["aria-haspopup"] == "menu"
        assert toggle["aria-label"]
        assert toggle[":aria-expanded"] == "open.toString()"
        assert toggle[":aria-controls"] == split.select_one('[role="menu"]')[":id"]
        assert split["x-data"] == "menuButton"
        assert split["@keydown.escape"] == "onEscape($event)"

    def test_component_script_is_loaded(self, member_client):
        assert "core/js/menu-button.js" in member_client.get(reverse("dashboard")).content.decode()

    def test_rail_hides_the_label_but_keeps_the_icon(self, member_client):
        main = _soup(member_client).select_one('[data-testid="nav-new-chat"]')
        assert "sidebar__cta" in main["class"]
        assert main.select_one(".sidebar__collapsible").get_text(strip=True) == "New chat"

    def test_sidebar_no_longer_routes_through_the_chooser(self, member_client):
        sidebar = _soup(member_client).select_one('[data-testid="app-sidebar"]')
        assert not sidebar.select(f'a[href="{reverse("session_new")}"]')


@pytest.mark.django_db
class TestMobileTopBarNew:
    def test_top_bar_holds_the_new_split_button(self, member_client):
        bar = _soup(member_client).select_one('[data-testid="mobile-top-bar"]')
        split = bar.select_one('[data-testid="topbar-new"]')
        assert "new-split--compact" in split["class"]

    def test_only_one_top_bar_copy(self, member_client):
        assert len(_soup(member_client).select('[data-testid="topbar-new"]')) == 1


@pytest.mark.django_db
class TestCollapseToggle:
    def test_sits_by_the_brand_and_controls_the_sidebar(self, member_client):
        soup = _soup(member_client)
        toggle = soup.select_one('[data-testid="sidebar-toggle"]')
        assert toggle.name == "button"
        assert toggle["type"] == "button"
        assert "sidebar__brand" in toggle.parent["class"]
        assert toggle["aria-controls"] == "app-sidebar"
        assert soup.select_one("#app-sidebar")["data-testid"] == "app-sidebar"
        assert toggle["@click"] == "$store.sidebar.toggle()"

    def test_reports_its_state_and_starts_expanded(self, member_client):
        toggle = _soup(member_client).select_one('[data-testid="sidebar-toggle"]')
        assert toggle["aria-expanded"] == "true"
        assert toggle[":aria-expanded"] == "(!$store.sidebar.rail).toString()"
        assert toggle["aria-keyshortcuts"] == "["

    def test_names_the_action_for_each_state(self, member_client):
        """The rail tier swaps which name is rendered, so the accessible name is right
        before Alpine boots as well."""
        toggle = _soup(member_client).select_one('[data-testid="sidebar-toggle"]')
        assert toggle.select_one(".sidebar__collapsible.sr-only").get_text() == "Collapse sidebar"
        assert toggle.select_one(".sidebar__rail-only.sr-only").get_text() == "Expand sidebar"
        assert "Expand sidebar" in toggle[":title"]
        assert toggle.select_one(".sidebar__toggle-icon")

    def test_only_shown_from_lg_up(self, member_client):
        classes = _soup(member_client).select_one('[data-testid="sidebar-toggle"]')["class"]
        assert {"hidden", "lg:inline-flex"} <= set(classes)

    def test_label_is_translated(self, member_client):
        with catalog({"Collapse sidebar": "Recolher barra lateral", "Expand sidebar": "Expandir barra lateral"}):
            toggle = _soup(member_client, "dashboard").select_one('[data-testid="sidebar-toggle"]')
        assert toggle.select_one(".sidebar__collapsible").get_text() == "Recolher barra lateral"
        assert "Expandir barra lateral" in toggle[":title"]

    def test_the_mobile_sheet_has_no_toggle_or_duplicate_id(self, member_client):
        soup = _soup(member_client)
        assert len(soup.select('[data-testid="app-sidebar"]')) == 2
        assert len(soup.select("#app-sidebar")) == 1
        assert len(soup.select('[data-testid="sidebar-toggle"]')) == 1

    def test_the_shortcut_and_store_are_wired_into_the_shell(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        assert "core/js/sidebar.js" in content
        assert '@keydown.window="$store.sidebar.onKey($event)"' in content

    @pytest.mark.parametrize(("url_name", "at_least"), [("dashboard", 7), ("sandbox_envs:list", 13)])
    def test_every_rail_icon_keeps_a_name_and_tooltip(self, admin_client, url_name, at_least):
        """The rail hides each label, so the `title` is what names the icon and shows on hover."""
        sidebar = _soup(admin_client, url_name).select_one("#app-sidebar")
        links = sidebar.select(".sidebar__nav-item, .sidebar__footer-link, .sidebar__cta")
        untitled = [link["href"] for link in links if not link.get("title", "").strip()]
        assert len(links) >= at_least
        assert not untitled
