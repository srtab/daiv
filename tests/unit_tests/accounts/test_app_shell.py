"""Guards for the app shell's three tiers — sheet + tab bar, icon rail, full sidebar.

Each tier is built from a hook the template has to opt into, so the failure mode is
silent: a nav item added without `sidebar__label` renders its text into a 4rem rail,
and a `<main>` that pads by a literal instead of `--app-tabbar-height` puts the chat
dock under the tab bar. Both only show up between 768px and 1023px, or on a phone.
"""

from __future__ import annotations

import re

from django.test import Client
from django.urls import reverse

import pytest

from accounts.models import User
from tests.unit_tests.test_template_comments import DAIV_DIR, iter_template_files

INPUT_CSS = DAIV_DIR / "static_src" / "css" / "input.css"
SIDEBAR_TEMPLATE = DAIV_DIR / "accounts" / "templates" / "accounts" / "_sidebar.html"
BASE_APP_TEMPLATE = DAIV_DIR / "accounts" / "templates" / "base_app.html"

RAIL_BLOCK = re.compile(r"@media \(min-width: 768px\) and \(max-width: 1023px\) \{(.*?)\n\}\n", re.DOTALL)
SIDEBAR_HOOK = re.compile(r"sidebar__[\w-]+")
NAV_ITEM = re.compile(r'class="sidebar__nav-item[^"]*"(.*?)</a>', re.DOTALL)
FONT_CDN = re.compile(r"https://fonts\.(?:googleapis|gstatic)\.com")


@pytest.fixture
def member(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="x123456789")  # noqa: S106


def test_every_sidebar_hook_is_handled_by_the_rail_tier():
    """The rail is the only tier that reads these hooks, so an unhandled one is dead
    markup at best and an overflowing label at worst. Discovered from the template: a
    new hook has to be enrolled in the media block, not just in an allowlist here."""
    rail = RAIL_BLOCK.search(INPUT_CSS.read_text(encoding="utf-8"))

    assert rail, "no 768–1023px rail block in input.css — the sidebar shows full labels at tablet widths"

    used = set(SIDEBAR_HOOK.findall(SIDEBAR_TEMPLATE.read_text(encoding="utf-8")))
    handled = set(SIDEBAR_HOOK.findall(rail.group(1)))

    assert used - handled == set(), f"the rail tier ignores {sorted(used - handled)}"
    assert handled - used == set(), (
        f"the rail tier styles hooks the sidebar no longer carries: {sorted(handled - used)}"
    )


def test_every_sidebar_nav_item_labels_its_text():
    """`sidebar__label` is what the rail hides. Without it the item's text renders into
    a 4rem-wide column, which is how the label spills over the icon."""
    unlabelled = [
        body.strip().splitlines()[0].strip()
        for body in NAV_ITEM.findall(SIDEBAR_TEMPLATE.read_text(encoding="utf-8"))
        if "sidebar__label" not in body
    ]

    assert not unlabelled, "sidebar items whose text can overflow the icon rail:\n" + "\n".join(unlabelled)


def test_the_chat_surface_pads_by_the_tab_bar_it_dodges():
    """The bar is `position: fixed`, so only `<main>`'s padding keeps the chat's sticky
    dock off it. Both halves read the same token rather than restating 3.5rem."""
    base_app = BASE_APP_TEMPLATE.read_text(encoding="utf-8")
    css = INPUT_CSS.read_text(encoding="utf-8")

    tab_bar = re.search(r'<nav data-testid="mobile-tab-bar".*?>', base_app, re.DOTALL)

    assert tab_bar, "base_app.html no longer renders the mobile tab bar"
    assert "h-(--app-tabbar-height)" in tab_bar.group(), "the tab bar sizes itself off-token"
    assert "pb-(--app-tabbar-height)" in base_app, "<main> no longer reserves the tab bar's height"
    assert "padding-bottom: var(--app-tabbar-height) !important" in css, (
        "the chat surface drops <main>'s padding below `md` — its dock now sits under the tab bar"
    )


def test_no_template_loads_a_font_from_a_cdn():
    """Geist is self-hosted (see the `@font-face` blocks in input.css). A CDN link
    reintroduces a third-party request and a FOUT the local files don't have."""
    offenders = [
        str(path.relative_to(DAIV_DIR))
        for path in iter_template_files()
        if path.suffix == ".html" and FONT_CDN.search(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "templates loading a font CDN: " + ", ".join(offenders)


@pytest.mark.django_db
def test_the_tab_bar_reaches_every_app_page(member):
    client = Client()
    client.force_login(member)

    for url_name in ("dashboard", "session_list", "schedule_list"):
        content = client.get(reverse(url_name)).content.decode()

        assert 'data-testid="mobile-tab-bar"' in content, f"{url_name} renders no tab bar"
        assert 'class="fixed inset-x-0 bottom-0 z-30 flex h-(--app-tabbar-height)' in content
