"""Guards for the app shell's three tiers — sheet + tab bar, icon rail, full sidebar.

Each tier is built from something the template has to opt into, so the failure mode is
silent: a nav item added without `sidebar__collapsible` renders its text into a 4rem rail,
and a `<main>` that pads by a literal instead of `--app-tabbar-height` puts the chat dock
under the tab bar. Both only show up between 768px and 1023px, or on a phone.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_picker_popovers import BASE_APP_TEMPLATE, INPUT_CSS, SIDEBAR_TEMPLATE
from tests.unit_tests.test_template_comments import DAIV_DIR, iter_template_files

RAIL_BLOCK = re.compile(
    r"@media \(width >= theme\(--breakpoint-md\)\) and \(width < theme\(--breakpoint-lg\)\) \{(.*?)\n\}\n", re.DOTALL
)
SIDEBAR_HOOK = re.compile(r"sidebar__[\w-]+")
NAV_ITEM = re.compile(r'class="sidebar__nav-item[^"]*"(.*?)</a>', re.DOTALL)
FONT_CDN = re.compile(r"https://fonts\.(?:googleapis|gstatic)\.com")


def test_every_sidebar_hook_is_handled_by_the_rail_tier():
    """The rail is the only tier that reads these hooks, so an unhandled one is dead markup
    at best and an overflowing label at worst. Discovered from the template: a new hook has
    to be enrolled in the media block, not just in an allowlist here.

    The block also carries no `width`: the rail's width is `--app-sidebar-width`, which the
    sheet inset reads. A `width` here would make the sidebar two widths again, and sheets
    would inset for the wrong one."""
    rail = RAIL_BLOCK.search(INPUT_CSS.read_text(encoding="utf-8"))

    assert rail, "no rail block bounded by the md/lg breakpoint tokens — the sidebar shows full labels at tablet widths"
    assert "width:" not in rail.group(1), "the rail restates the sidebar's width instead of tiering --app-sidebar-width"

    used = set(SIDEBAR_HOOK.findall(SIDEBAR_TEMPLATE.read_text(encoding="utf-8")))
    handled = set(SIDEBAR_HOOK.findall(rail.group(1)))

    assert used - handled == set(), f"the rail tier ignores {sorted(used - handled)}"
    assert handled - used == set(), (
        f"the rail tier styles hooks the sidebar no longer carries: {sorted(handled - used)}"
    )


def test_every_sidebar_nav_item_labels_its_text():
    """`sidebar__collapsible` is what the rail hides. Without it the item's text renders
    into a 4rem-wide column, which is how the label spills over the icon."""
    unlabelled = [
        body.strip().splitlines()[0].strip()
        for body in NAV_ITEM.findall(SIDEBAR_TEMPLATE.read_text(encoding="utf-8"))
        if "sidebar__collapsible" not in body
    ]

    assert not unlabelled, "sidebar items whose text can overflow the icon rail:\n" + "\n".join(unlabelled)


def test_the_chat_surface_pads_by_the_tab_bar_it_dodges():
    """The bar is `position: fixed`, so only `<main>`'s padding keeps the chat's sticky dock
    off it. `<main>` owns that clearance for every page; the chat rule only drops it from
    `md:` up, where the bar is gone. Both halves read the token rather than restating 3.5rem."""
    base_app = BASE_APP_TEMPLATE.read_text(encoding="utf-8")
    css = INPUT_CSS.read_text(encoding="utf-8")

    tab_bar = re.search(r'<nav data-testid="mobile-tab-bar".*?>', base_app, re.DOTALL)

    assert tab_bar, "base_app.html no longer renders the mobile tab bar"
    assert "h-(--app-tabbar-height)" in tab_bar.group(), "the tab bar sizes itself off-token"
    assert "pb-(--app-tabbar-height)" in base_app, "<main> no longer reserves the tab bar's height"
    assert re.search(
        r"@media \(width >= theme\(--breakpoint-md\)\) \{\s*main:has\(\.chat-shell\) \{\s*padding-bottom: 0", css
    ), "the chat surface drops <main>'s padding outside the `md:` block — its dock now sits under the tab bar"


def test_no_template_loads_a_font_from_a_cdn():
    """Geist is self-hosted (see the `@font-face` blocks in input.css). A CDN link
    reintroduces a third-party request and a FOUT the local files don't have."""
    offenders = [
        str(path.relative_to(DAIV_DIR))
        for path in iter_template_files()
        if path.suffix == ".html" and FONT_CDN.search(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "templates loading a font CDN: " + ", ".join(offenders)
