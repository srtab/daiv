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

# Alpine components that own a `.picker-popover` or a composer sheet — every surface that
# below 1100px renders pinned to the bottom edge, where a second one would stack. The count
# below is the tripwire: an allowlist alone can only catch a regression in what it already
# lists, never the new surface that forgot to enrol.
SURFACE_SCRIPTS = (
    "sandbox_envs/static/sandbox_envs/js/env-picker.js",
    "automation/static/automation/js/agent-picker.js",
    "sessions/static/sessions/js/prompt-box.js",
    "schedules/static/schedules/js/subscriber-picker.js",
    "chat/static/chat/js/chat-stream.js",
)
EXPECTED_SURFACES = 7

# The popover container itself. The negative lookahead drops `picker-popover__search` and
# `__list`, which are content *inside* a popover and carry their own utilities.
CONTAINER = re.compile(r'class="(picker-popover(?![_\w])[^"]*)"')

SHEET_HEAD = "core/_sheet_head.html"


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
            found += path.read_text(encoding="utf-8").count('class="composer-sheet ')

    assert found == EXPECTED_SURFACES, (
        f"floating surfaces went from {EXPECTED_SURFACES} to {found} — enrol the new one in "
        f"`surfaceGroup` (see core/js/surface-group.js), add its script to SURFACE_SCRIPTS, and "
        f"update EXPECTED_SURFACES"
    )


def test_the_group_helper_loads_before_the_component_definitions():
    """Alpine starts on the microtask after its own tag, so a script placed lower runs
    after every `init()` — `surfaceGroup` would be undefined there."""
    source = (DAIV_DIR / "accounts/templates/base.html").read_text(encoding="utf-8")
    helper = source.find("core/js/surface-group.js")
    block = source.find("{% block alpine_plugins %}")

    assert helper != -1, "base.html no longer loads core/js/surface-group.js"
    assert helper < block, "surface-group.js must load ahead of the alpine_plugins block"
