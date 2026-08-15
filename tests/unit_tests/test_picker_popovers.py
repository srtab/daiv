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

from tests.unit_tests.test_template_comments import iter_template_files

# The popover container itself. The negative lookahead drops `picker-popover__search` and
# `__list`, which are content *inside* a popover and carry their own utilities.
CONTAINER = re.compile(r'class="(picker-popover(?![_\w])[^"]*)"')

SHEET_HEAD = "core/_picker_sheet_head.html"


def _templates_with_popovers() -> dict[str, str]:
    return {
        str(path): source
        for path in iter_template_files()
        if path.suffix == ".html" and 'class="picker-popover' in (source := path.read_text(encoding="utf-8"))
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

    assert len(found) >= 4, f"expected pickers from several apps, found {sorted(found)}"
    assert sum(len(CONTAINER.findall(source)) for source in found.values()) >= 5
