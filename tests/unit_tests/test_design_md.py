"""DESIGN.md's front matter mirrors the `@theme` colours in `input.css`, which owns them."""

from __future__ import annotations

import re

import yaml
from skills.constants import FRONTMATTER_RE

from tests.unit_tests.test_surface_contrast import theme_colours
from tests.unit_tests.test_template_comments import REPO_ROOT

DESIGN_MD = REPO_ROOT / "DESIGN.md"

COLOUR_REFERENCE = re.compile(r"\{colors\.([\w-]+)\}")


def design_colours() -> dict[str, str]:
    match = FRONTMATTER_RE.match(DESIGN_MD.read_text(encoding="utf-8"))
    assert match, "DESIGN.md has no YAML front matter"
    return yaml.safe_load(match.group(1))["colors"]


def test_front_matter_declares_every_theme_colour_with_its_value():
    theme = {name.removeprefix("--color-"): value.lower() for name, value in theme_colours().items()}
    declared = design_colours()
    resolved = {
        name: (declared[reference[1]] if (reference := COLOUR_REFERENCE.fullmatch(value)) else value).lower()
        for name, value in declared.items()
    }

    assert {name: resolved.get(name) for name in theme} == theme


def test_front_matter_colours_outside_the_theme_are_aliases():
    theme = {name.removeprefix("--color-") for name in theme_colours()}

    assert [
        name for name, value in design_colours().items() if name not in theme and not COLOUR_REFERENCE.fullmatch(value)
    ] == []
