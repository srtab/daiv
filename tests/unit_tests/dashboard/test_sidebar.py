import uuid

from django.urls import reverse

import pytest
from bs4 import BeautifulSoup
from notifications.models import Notification
from sessions.models import Run, RunStatus, Session, SessionOrigin

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
        content = member_client.get(reverse("session_list")).content.decode()
        link = BeautifulSoup(content, "html.parser").select_one('[data-testid="sidebar-docs-link"]')
        assert link["href"] == "https://srtab.github.io/daiv/latest/"
        assert link["target"] == "_blank"
        assert "sidebar__footer-link" in link["class"]
        assert link.select_one(".sidebar__collapsible").get_text() == "Help & docs"

    def test_label_is_translated(self, member_client):
        with catalog({"Help & docs": "Ajuda"}):
            content = member_client.get(reverse("dashboard")).content.decode()
        assert '<span class="sidebar__collapsible">Ajuda</span>' in content


@pytest.mark.django_db
class TestMobileTopBar:
    def test_opens_the_sidebar_sheet_and_shows_unread(self, member_client):
        content = member_client.get(reverse("dashboard")).content.decode()
        bar = content.split('data-testid="mobile-top-bar"', 1)[1].split("</header>", 1)[0]
        assert "md:hidden" in bar.split(">", 1)[0]
        assert '@click="mobileNavOpen = !mobileNavOpen"' in bar
        assert 'x-show="$store.nav.unread > 0"' in bar


@pytest.mark.django_db
class TestAdminGroupVisibility:
    def test_admin_sees_admin_group(self, admin_client):
        response = admin_client.get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' in response.content
        assert b"Users" in response.content
        assert b"Configuration" in response.content

    def test_member_does_not_see_admin_group(self, member_client):
        response = member_client.get(reverse("dashboard"))
        assert b'data-testid="nav-admin-group"' not in response.content


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
class TestArtifactsNavItem:
    def test_sidebar_shows_artifacts_link(self, member_client):
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        assert reverse("artifact_list") in content
        assert "Artifacts" in content


@pytest.mark.django_db
class TestRunningJobsBadge:
    """The badge's text is Alpine-bound to the `nav` store, so what the server controls
    is the seed handed to `$store.nav.start(...)` and the store expressions on the badge
    — the count itself is covered in test_context_processors.py."""

    def test_badge_is_bound_to_the_store_not_server_rendered(self, member_client):
        response = member_client.get(reverse("dashboard"))
        content = response.content.decode()
        # Present at any count (x-show hides it at zero), so a 0 → 1 transition has an
        # element to reveal without a page load.
        assert 'data-testid="nav-running-badge"' in content
        assert 'x-show="$store.nav.running > 0"' in content
        assert 'x-text="$store.nav.runningLabel"' in content

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


def _soup(client, url_name="session_list"):
    return BeautifulSoup(client.get(reverse(url_name)).content.decode(), "html.parser")


def _sidebar_nav(client, url_name="session_list"):
    """The persistent sidebar's <nav>; the mobile sheet renders a second copy of the partial."""
    return _soup(client, url_name).select_one('[data-testid="app-sidebar"] nav')


def _item_label(item) -> str:
    label = item.select_one(".sidebar__collapsible")
    return (label.find("span") or label).get_text(strip=True)


def _nav_outline(nav) -> list[str]:
    """Headings as ``# Heading`` and items by label, in document order."""
    outline = []
    for node in nav.find_all(["a", "div"], recursive=False):
        if "sidebar__group-heading" in node.get("class", []):
            outline.append("# " + node.select_one(".sidebar__collapsible").get_text(strip=True))
        else:
            outline.append(_item_label(node))
    return outline


DAILY_WORK = ["Home", "Sessions", "Notifications", "Artifacts", "Schedules", "Insights"]
AGENT_SETUP = ["# Agent setup", "Sandbox environments", "MCP servers", "Memory"]
ADMIN = ["# Admin", "Users", "Configuration", "Schedule templates", "Skills", "Global MCP servers"]


@pytest.mark.django_db
class TestSidebarGroups:
    def test_member_sees_daily_work_then_agent_setup(self, member_client):
        nav = _sidebar_nav(member_client)
        assert _nav_outline(nav) == DAILY_WORK + AGENT_SETUP

    def test_admin_group_comes_last(self, admin_client):
        nav = _sidebar_nav(admin_client)
        assert _nav_outline(nav) == DAILY_WORK + AGENT_SETUP + ADMIN

    def test_home_links_the_dashboard(self, member_client):
        nav = _sidebar_nav(member_client)
        home = next(a for a in nav.find_all("a", recursive=False) if _item_label(a) == "Home")
        assert home["href"] == reverse("dashboard")

    def test_agent_setup_items_say_what_they_are_for(self, member_client):
        nav = _sidebar_nav(member_client)
        described = nav.select(".sidebar__nav-item--described")
        assert [_item_label(a) for a in described] == AGENT_SETUP[1:]
        for item in described:
            description = item.select_one('[data-testid="nav-description"]').get_text(strip=True)
            assert description
            # The rail hides the description, so the tooltip has to carry it.
            assert item["title"] == f"{_item_label(item)}: {description}"

    def test_agent_setup_heading_says_it_is_optional(self, member_client):
        nav = _sidebar_nav(member_client)
        heading = nav.select_one('[data-testid="nav-setup-group"]')
        assert "Optional" in heading.get_text()
        assert heading["title"].startswith("Optional")

    def test_mobile_sheet_shows_the_same_groups(self, member_client):
        sheet_nav = _soup(member_client).select('[data-testid="app-sidebar"]')[1].select_one("nav")
        assert _nav_outline(sheet_nav) == DAILY_WORK + AGENT_SETUP

    @pytest.mark.parametrize(
        "url_name,label",
        [
            ("dashboard", "Home"),
            ("insights", "Insights"),
            ("session_list", "Sessions"),
            ("sandbox_envs:list", "Sandbox environments"),
            ("mcp_connectors:list", "MCP servers"),
            ("memory:list", "Memory"),
        ],
    )
    def test_the_active_item_is_the_page_section(self, member_client, url_name, label):
        nav = _sidebar_nav(member_client, url_name)
        active = nav.select(".sidebar__nav-item--active")
        assert [_item_label(a) for a in active] == [label]


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

    def test_every_rail_icon_keeps_a_name_and_tooltip(self, admin_client):
        """The rail hides each label, so the `title` is what names the icon and shows on hover."""
        sidebar = _soup(admin_client, "dashboard").select_one("#app-sidebar")
        links = sidebar.select(".sidebar__nav-item, .sidebar__footer-link, .sidebar__cta")
        untitled = [link["href"] for link in links if not link.get("title", "").strip()]
        assert len(links) > 10
        assert not untitled
