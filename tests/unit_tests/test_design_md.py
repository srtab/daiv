"""DESIGN.md's front matter mirrors the `@theme` colours in `input.css`, which owns them.

Lives at the top of tests/unit_tests/ like ``test_surface_contrast``: DESIGN.md belongs to
no app. ``make lint-design`` checks the file against the design.md spec; this checks it
against the stylesheet that ships.
"""

from __future__ import annotations

import re

import yaml

from tests.unit_tests.test_picker_popovers import INPUT_CSS
from tests.unit_tests.test_surface_contrast import theme_colours
from tests.unit_tests.test_template_comments import REPO_ROOT

DESIGN_MD = REPO_ROOT / "DESIGN.md"

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
COLOUR_REFERENCE = re.compile(r"\{colors\.([\w-]+)\}")
CONTENT_GUTTER = re.compile(r"--app-content-gutter:\s*([^;]+);")


def front_matter() -> dict:
    match = FRONT_MATTER.match(DESIGN_MD.read_text(encoding="utf-8"))
    assert match, "DESIGN.md has no YAML front matter"
    return yaml.safe_load(match.group(1))


def design_colours() -> dict[str, str]:
    return front_matter()["colors"]


def resolve(declared: dict[str, str]) -> dict[str, str]:
    """Each colour as a lowercase hex, following `{colors.*}` references to their literal."""
    resolved = {}
    for name, value in declared.items():
        seen = {name}
        while reference := COLOUR_REFERENCE.fullmatch(value):
            target = reference.group(1)
            assert target not in seen, f"colors.{name} references itself through colors.{target}"
            seen.add(target)
            value = declared[target]
        resolved[name] = value.lower()
    return resolved


def test_front_matter_declares_every_theme_colour_with_its_value():
    theme = {name.removeprefix("--color-"): value.lower() for name, value in theme_colours().items()}
    declared = resolve(design_colours())

    assert {name: declared.get(name) for name in theme} == theme


def test_front_matter_colours_outside_the_theme_are_aliases():
    """`primary`, which the spec requires, names a theme colour instead of adding one."""
    theme = {name.removeprefix("--color-") for name in theme_colours()}
    extras = {name: value for name, value in design_colours().items() if name not in theme}

    assert {name: value for name, value in extras.items() if not COLOUR_REFERENCE.fullmatch(value)} == {}


def test_front_matter_gutter_is_the_shell_gutter():
    gutter = CONTENT_GUTTER.search(INPUT_CSS.read_text(encoding="utf-8")).group(1).strip()

    assert front_matter()["spacing"]["gutter"] == gutter
