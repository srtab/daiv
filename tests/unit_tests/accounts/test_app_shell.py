"""Guards for the app shell's three tiers — mobile sheet, icon rail, full sidebar.

Each tier is built from something the template has to opt into, so the failure mode is
silent: a nav item added without `sidebar__collapsible` renders its text into a 4rem rail,
which only shows up between 768px and 1023px.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_picker_popovers import INPUT_CSS, SIDEBAR_TEMPLATE
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


def test_the_account_menu_is_not_clipped_by_the_sidebar():
    """An `overflow` on the `<aside>` clips anything absolutely positioned inside it, and the
    account menu opens past the rail's right edge. Only the `<nav>` list may scroll, and the
    menu has to live after it."""
    sidebar = SIDEBAR_TEMPLATE.read_text(encoding="utf-8")
    aside = re.search(r"<aside\s[^>]*>", sidebar)
    nav = re.search(r"<nav\s[^>]*>", sidebar)

    assert aside and nav, "_sidebar.html no longer opens an <aside> holding a <nav>"
    assert "overflow" not in aside.group(), "the sidebar scrolls as a whole and clips the account menu"
    assert "overflow-y-auto" in nav.group(), (
        "the nav list no longer scrolls — a long admin nav pushes the menu off-screen"
    )
    assert sidebar.index('data-testid="app-user-menu"') > sidebar.index("</nav>"), (
        "the account menu sits inside the scrolling nav, which clips it"
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
