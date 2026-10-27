"""Guards for the app shell's three tiers — mobile sheet, icon rail, full sidebar.

Each tier is built from something the template has to opt into, so the failure mode is
silent: a nav item added without `sidebar__collapsible` renders its text into a 4rem rail,
which only shows up between 768px and 1023px.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_picker_popovers import INPUT_CSS, SIDEBAR_TEMPLATE
from tests.unit_tests.test_template_comments import DAIV_DIR, iter_template_files

RAIL_VARIANT = re.compile(r"@custom-variant sidebar-rail \{(.*?)\n\}\n", re.DOTALL)
RAIL_BLOCK = re.compile(r"\.sidebar--rail \{\n  @variant sidebar-rail \{(.*?)\n  \}\n\}\n", re.DOTALL)
WIDE_TIER = re.compile(r"^@media \(width >= theme\(--breakpoint-lg\)\) \{(.*?)\n\}\n", re.DOTALL | re.MULTILINE)
HEAD_SCRIPT = re.compile(r"<head>.*?<script>(.*?)</script>", re.DOTALL)
BASE_TEMPLATE = DAIV_DIR / "core" / "templates" / "base.html"
SIDEBAR_JS = DAIV_DIR / "core" / "static" / "core" / "js" / "sidebar.js"
SIDEBAR_HOOK = re.compile(r"sidebar__[\w-]+")
NAV_ITEM = re.compile(r'class="sidebar__nav-item[^"]*"(.*?)</a>', re.DOTALL)
INCLUDE = re.compile(r'{% include "dashboard/([\w.]+)"')
FONT_CDN = re.compile(r"https://fonts\.(?:googleapis|gstatic)\.com")


def _sidebar_sources() -> str:
    """The sidebar and every dashboard partial it includes."""
    sidebar = SIDEBAR_TEMPLATE.read_text(encoding="utf-8")
    partials = {SIDEBAR_TEMPLATE.with_name(name) for name in INCLUDE.findall(sidebar)}
    return "\n".join([sidebar, *(path.read_text(encoding="utf-8") for path in sorted(partials))])


def test_every_sidebar_hook_is_handled_by_the_rail_tier():
    """The rail is the only tier that reads these hooks, so an unhandled one is dead markup
    at best and an overflowing label at worst. Discovered from the template: a new hook has
    to be enrolled in the media block, not just in an allowlist here.

    The block also carries no `width`: the rail's width is `--app-sidebar-width`, which the
    sheet inset reads. A `width` here would make the sidebar two widths again, and sheets
    would inset for the wrong one."""
    rail = RAIL_BLOCK.search(INPUT_CSS.read_text(encoding="utf-8"))

    assert rail, "no `.sidebar--rail` block under the sidebar-rail variant — the sidebar shows full labels on the rail"
    assert "width:" not in rail.group(1), "the rail restates the sidebar's width instead of tiering --app-sidebar-width"

    used = set(SIDEBAR_HOOK.findall(_sidebar_sources()))
    handled = set(SIDEBAR_HOOK.findall(rail.group(1)))

    assert used - handled == set(), f"the rail tier ignores {sorted(used - handled)}"
    assert handled - used == set(), (
        f"the rail tier styles hooks the sidebar no longer carries: {sorted(handled - used)}"
    )


def test_the_rail_is_forced_at_tablet_widths_and_opt_in_from_lg():
    """One set of rail rules, reached two ways: always between md and lg, and from lg up only
    under the collapse flag. A third copy of the rules would drift from these."""
    css = INPUT_CSS.read_text(encoding="utf-8")
    variant = RAIL_VARIANT.search(css)

    assert variant, "no sidebar-rail variant — the rail rules have no shared entry point"
    branches = variant.group(1)
    assert "@media (width >= theme(--breakpoint-md)) and (width < theme(--breakpoint-lg))" in branches
    assert re.search(r'@media \(width >= theme\(--breakpoint-lg\)\) \{\s*:root\[data-sidebar="rail"\] &', branches), (
        "the collapse flag no longer reaches the rail rules from lg up"
    )
    assert css.count("@variant sidebar-rail") == 1, "the rail rules are split across copies"


def test_the_collapsed_flag_narrows_the_one_sidebar_width_token():
    """The sheets' inset reads `--app-sidebar-width`, so the collapse has to change the token
    rather than the aside's own width."""
    wide = WIDE_TIER.search(INPUT_CSS.read_text(encoding="utf-8"))

    assert wide, "no lg block widening the sidebar"
    assert re.search(r":root \{\s*--app-sidebar-width: 15rem;\s*\}", wide.group(1))
    assert re.search(r':root\[data-sidebar="rail"\] \{\s*--app-sidebar-width: 4rem;\s*\}', wide.group(1))


def test_the_head_restores_the_collapse_before_first_paint():
    """Inline and ahead of the stylesheet, or a collapsed sidebar renders full and then snaps
    to the rail. It reads the same storage key the store writes."""
    base = BASE_TEMPLATE.read_text(encoding="utf-8")
    script = HEAD_SCRIPT.search(base)
    key = re.search(r'STORAGE_KEY = "([^"]+)"', SIDEBAR_JS.read_text(encoding="utf-8"))

    assert script, "base.html's <head> has no inline script restoring the sidebar"
    assert base.index(script.group(0)) < base.index("css/styles.css")
    assert key and f'localStorage.getItem("{key.group(1)}") === "rail"' in script.group(1)
    assert 'document.documentElement.dataset.sidebar = "rail"' in script.group(1)
    assert script.group(1).lstrip().startswith("try {"), "blocked storage would throw before first paint"


def test_every_sidebar_nav_item_labels_its_text():
    """`sidebar__collapsible` is what the rail hides. Without it the item's text renders
    into a 4rem-wide column, which is how the label spills over the icon."""
    unlabelled = [
        body.strip().splitlines()[0].strip()
        for body in NAV_ITEM.findall(_sidebar_sources())
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
    assert sidebar.index("_new_split.html") < sidebar.index("<nav"), (
        "the New menu sits inside the scrolling nav, which clips it on the rail"
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
