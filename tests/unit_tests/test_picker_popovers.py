"""Repository-wide guards for the picker popovers.

Lives beside `test_template_comments.py` rather than in an app package: pickers ship from
five apps and the rules below only hold if *every* one of them follows them.

A `.picker-popover` is an anchored popover at >=1100px and a bottom sheet below it. Both
halves are fragile in the same way — a call site that re-states geometry as a Tailwind
utility silently wins over the component rules (the utilities layer outranks the
components layer whatever the selector), which is what once left the model popover
hanging off the right edge of a phone.
"""

from __future__ import annotations

import re

from tests.unit_tests.test_template_comments import DAIV_DIR, iter_template_files

# Alpine components that own a floating surface — one that opens over the page and so has
# to take the single `surfaceGroup` slot, whether it renders as a bottom sheet, an anchored
# popover, or the composer-width autocomplete. The count below is the tripwire: an allowlist
# alone can only catch a regression in what it already lists, never the new surface that
# forgot to enrol.
SURFACE_SCRIPTS = (
    "sandbox_envs/static/sandbox_envs/js/env-picker.js",
    "automation/static/automation/js/agent-picker.js",
    "sessions/static/sessions/js/prompt-box.js",
    "schedules/static/schedules/js/subscriber-picker.js",
    "chat/static/chat/js/chat-stream.js",
    "core/static/core/js/config-section-picker.js",
)
EXPECTED_SURFACES = 9

# Surfaces that carry their own container class (`.picker-popover` is matched by CONTAINER
# instead). A new kind of surface belongs here, not just on the `surface-rise` roster. The
# lookahead keeps the match on the container itself, so `composer-sheet__body` and
# `composer-sheet-anchor` don't count and adding a modifier to a container doesn't stop it
# counting.
SURFACE_CLASSES = ("composer-sheet", "composer-autocomplete")
SURFACE_CLASS = re.compile(r'class="(?:{})(?![-\w])'.format("|".join(SURFACE_CLASSES)))

# The `surface-rise` roster by name — every class that *is* a floating surface container,
# which is what a guard on their z-order or their ancestors has to look for. `.picker-popover`
# is matched by CONTAINER rather than SURFACE_CLASS, and the two dropdowns are built from
# utilities, so neither reaches SURFACE_CLASSES above.
SURFACE_CONTAINERS = frozenset({*SURFACE_CLASSES, "picker-popover", "filter-menu", "card__menu-panel"})

# The popover container itself. The negative lookahead drops `picker-popover__search` and
# `__list`, which are content *inside* a popover and carry their own utilities.
POPOVER_CLASS = r'class="(picker-popover(?![_\w])[^"]*)"'
CONTAINER = re.compile(POPOVER_CLASS)
# The whole opening tag, for the attributes that have to agree with the scrim beside it.
# `[^>]` can't cross a tag boundary, so the blob is whatever the popover's own `<div` carries.
POPOVER_TAG = re.compile(rf"<div\s+([^>]*{POPOVER_CLASS}[^>]*)>")
X_SHOW = re.compile(r'x-show="([^"]*)"')

SHEET_HEAD = "core/_sheet_head.html"
BACKDROP_SHOW = re.compile(r'\{% include "core/_sheet_backdrop\.html" with show_expr="([^"]*)"')

INPUT_CSS = DAIV_DIR / "static_src" / "css" / "input.css"
BREAKPOINT_TOKEN = re.compile(r"--breakpoint-popover:\s*(\d+)px")
# Any single-condition breakpoint block, by its width alone: the file spells the same number
# `width < N`, `width >= N` and `min-width: N`, and a rule is no less pinned for the idiom it
# picked. Steps nest one level (the rules the block re-declares), so one alternation is enough.
MEDIA = re.compile(r"@media \([^)]*?(\d+)px\) \{((?:[^{}]|\{[^{}]*\})*)\}")

# The band where the sidebar is on screen and the surfaces are still bottom sheets. `40rem`
# rather than `640px` because that is what Tailwind compiles the sidebar's own `sm:` to.
SHEET_BAND = re.compile(r"@media \(width >= 40rem\) and \(width < (\d+)px\) \{((?:[^{}]|\{[^{}]*\})*)\}")
SHEET_CONTAINERS = ("composer-sheet", "picker-popover")
SIDEBAR_TEMPLATE = DAIV_DIR / "accounts" / "templates" / "accounts" / "_sidebar.html"
BASE_APP_TEMPLATE = DAIV_DIR / "accounts" / "templates" / "base_app.html"


def _token(css: str, name: str) -> str | None:
    found = re.search(rf"{name}:\s*([^;]+);", css)
    return found.group(1).strip() if found else None


def _templates_with_popovers() -> dict[str, str]:
    return {
        str(path): source
        for path in iter_template_files()
        if path.suffix == ".html" and CONTAINER.search(source := path.read_text(encoding="utf-8"))
    }


def test_popover_geometry_is_never_a_utility():
    """Geometry belongs to `.picker-popover` and its modifiers, so the container carries
    nothing else. A `left-*`/`w-*` on the element outranks every component rule, and the
    bottom sheet below 1100px then cannot take over from the desktop anchor."""
    offenders = [
        f"{rel}: {other}"
        for rel, source in _templates_with_popovers().items()
        for classes in CONTAINER.findall(source)
        if (other := " ".join(c for c in classes.split() if not c.startswith("picker-popover")))
    ]

    assert not offenders, "Popover geometry must live in CSS, not utilities on the element:\n" + "\n".join(offenders)


def test_every_popover_carries_a_sheet_dismiss():
    """As a bottom sheet the popover covers the trigger that would otherwise close it, so
    each one includes the shared head. Counted rather than merely present: a file with two
    popovers (repo + branch) needs two."""
    failures = [
        f"{rel}: {popovers} popover(s) but {heads} sheet head(s)"
        for rel, source in _templates_with_popovers().items()
        if (popovers := len(CONTAINER.findall(source))) != (heads := source.count(SHEET_HEAD))
    ]

    assert not failures, "Every .picker-popover needs its own sheet head:\n" + "\n".join(failures)


def test_every_popover_dims_the_page_behind_its_sheet():
    """A bottom sheet covers the page instead of floating over a corner of it, so without
    the scrim the page below reads as live while every click on it only dismisses. Matched
    by expression, not counted: a scrim on a condition the surface no longer uses shows the
    dim with nothing in front of it. The scrim carries no handler of its own — a click on it
    is outside the surface by construction, so `@click.outside` is what dismisses, and a
    surface without one would make the dim a trap."""
    failures = []
    for rel, source in _templates_with_popovers().items():
        tags = POPOVER_TAG.findall(source)
        popovers = sorted(shown[1] for attrs, _ in tags if (shown := X_SHOW.search(attrs)))
        if popovers != (scrims := sorted(BACKDROP_SHOW.findall(source))):
            failures.append(f"{rel}: popovers {popovers} vs scrims {scrims}")
        if undismissable := [attrs for attrs, _ in tags if "@click.outside" not in attrs]:
            failures.append(f"{rel}: {len(undismissable)} popover(s) with no @click.outside")

    assert not failures, (
        "Every .picker-popover needs a core/_sheet_backdrop.html shown by its own expression:\n" + "\n".join(failures)
    )


def test_the_sheet_breakpoint_is_a_single_number():
    """A layout that swaps with the surface — a sidebar giving way to the sheet that
    replaces it — spells the switch `popover:`, which Tailwind compiles from
    `--breakpoint-popover`. Let that drift from the media queries and a band opens where the
    trigger is showing while the surface is still an anchored, headless popover. The scrim is
    the same number from the other side: left on above it, it dims the whole app behind a
    popover anchored to a trigger the user can still see."""
    source = INPUT_CSS.read_text(encoding="utf-8")
    token = BREAKPOINT_TOKEN.search(source)

    assert token, "`@theme` no longer declares --breakpoint-popover"

    for rule in (".picker-popover {", ".sheet-backdrop {"):
        widths = {width for width, body in MEDIA.findall(source) if rule in body}

        assert widths == {token.group(1)}, (
            f"--breakpoint-popover is {token.group(1)}px but {rule.rstrip(' {')} switches at {widths or 'nowhere'}"
        )


def test_a_bottom_sheet_never_opens_under_the_sidebar():
    """A sheet flush to the viewport edges is only right while the viewport *is* the content
    area. From `sm:` up the sidebar is on screen while these are still sheets — tablets, and
    phones in landscape — so a full-bleed sheet spanned the nav too: Chromium paints it over
    the sidebar, and the browser this was reported from clipped the half that crossed the
    scrolling `<main>`, cutting every line off mid-word. The band insets both families to
    the content column and caps them, and it wins by *source order* alone — same specificity
    as the rules it re-declares, so moving it above either one silently does nothing."""
    css = INPUT_CSS.read_text(encoding="utf-8")
    band = SHEET_BAND.search(css)

    assert band, "no `sm:`-to-popover band in input.css — a bottom sheet spans the sidebar again"

    popover_breakpoint = BREAKPOINT_TOKEN.search(css)
    assert band.group(1) == popover_breakpoint.group(1), (
        f"the band ends at {band.group(1)}px but sheets become popovers at {popover_breakpoint.group(1)}px"
    )

    body = band.group(2)
    assert "var(--app-sidebar-width)" in body, "the inset must come from the sidebar's own token"
    assert "var(--sheet-max-width)" in body, "an inset sheet still spans a 1024px viewport without the cap"

    selectors = body[: body.index("{")]
    for surface in SHEET_CONTAINERS:
        assert re.search(rf"\.{surface}(?![-\w])", selectors), f".{surface} is not in the band"

        own_rules = re.finditer(rf"\.{surface}(?![-\w])\s*\{{", css)
        elsewhere = [m.start() for m in own_rules if not band.start() <= m.start() < band.end()]
        assert elsewhere and max(elsewhere) < band.start(), (
            f"the band must stay after every .{surface} rule it re-declares"
        )


def test_the_sheet_inset_tracks_the_shell_it_dodges():
    """The inset is the shell's own geometry restated for surfaces that can't measure it —
    `position: fixed` reads the viewport, not the column it belongs to. Both halves come
    from the shell: the sidebar sizes itself from the width token (a literal `w-60` back on
    the aside is a sheet under the nav again) and `<main>` carries the gutter (`px-6` is
    1.5rem), which is what lines the sheet up with the composer that opened it."""
    css = INPUT_CSS.read_text(encoding="utf-8")
    sidebar = SIDEBAR_TEMPLATE.read_text(encoding="utf-8")

    assert "w-(--app-sidebar-width)" in sidebar, "the sidebar no longer sizes itself from --app-sidebar-width"
    assert "sm:flex" in sidebar, "the sidebar appears at some width other than `sm:` — the band's lower bound moved"
    assert _token(css, "--app-content-gutter") == "1.5rem", "--app-content-gutter must be <main>'s `sm:px-6`"
    assert "sm:px-6" in BASE_APP_TEMPLATE.read_text(encoding="utf-8"), (
        "<main> no longer pads by px-6 at `sm:` — the sheet inset no longer matches the content column"
    )


def test_the_guards_are_actually_looking_at_something():
    """A typo in the container pattern would make both tests above vacuously pass."""
    found = _templates_with_popovers()

    assert sum(len(CONTAINER.findall(source)) for source in found.values()) >= 5, (
        f"expected the pickers from several apps, found {sorted(found)}"
    )


def test_every_surface_component_joins_the_group():
    """Triggers stop the opening click from reaching `document`, so a surface only learns
    that a neighbour opened through `surfaceGroup` — one that never announces leaves the
    others open, stacked on the same edge."""
    missing = []
    for path in SURFACE_SCRIPTS:
        source = (DAIV_DIR / path).read_text(encoding="utf-8")
        if absent := [call for call in ("surfaceGroup.join(", "_announceOpen") if call not in source]:
            missing.append(f"{path}: {', '.join(absent)}")

    assert not missing, "Every floating surface joins the group and announces its open:\n" + "\n".join(missing)


def test_a_new_surface_forces_a_look_at_the_group():
    """`SURFACE_SCRIPTS` lists the components that already comply, so on its own it can
    never fail for the surface that forgot to join. Counting the surfaces themselves is
    what makes adding one land here."""
    found = sum(len(CONTAINER.findall(source)) for source in _templates_with_popovers().values())
    for path in iter_template_files():
        if path.suffix == ".html":
            found += len(SURFACE_CLASS.findall(path.read_text(encoding="utf-8")))

    assert found == EXPECTED_SURFACES, (
        f"floating surfaces went from {EXPECTED_SURFACES} to {found} — enrol the new one in "
        f"`surfaceGroup` (see core/js/surface-group.js), add its script to SURFACE_SCRIPTS, and "
        f"update EXPECTED_SURFACES"
    )


def test_every_surface_class_is_on_the_surface_rise_roster():
    """A surface that appears without motion reads as a repaint glitch next to the ones that
    rise; membership is the one grouped rule at the top of input.css."""
    css = INPUT_CSS.read_text(encoding="utf-8")
    roster_start = css.index(".surface-rise,")
    entries = {part.strip() for part in css[roster_start : css.index("{", roster_start)].split(",")}

    missing = [name for name in sorted(SURFACE_CONTAINERS) if f".{name}" not in entries]
    assert not missing, f"not on the `surface-rise` roster in input.css: {missing}"


def test_the_group_helper_loads_before_the_component_definitions():
    """Alpine starts on the microtask after its own tag, so a script placed lower runs
    after every `init()` — `surfaceGroup` would be undefined there."""
    source = (DAIV_DIR / "accounts/templates/base.html").read_text(encoding="utf-8")
    helper = source.find("core/js/surface-group.js")
    block = source.find("{% block alpine_plugins %}")

    assert helper != -1, "base.html no longer loads core/js/surface-group.js"
    assert helper < block, "surface-group.js must load ahead of the alpine_plugins block"
