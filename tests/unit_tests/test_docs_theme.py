"""The docs stylesheet's `--daiv-*` colours mirror the `@theme` colours in `input.css`, which owns them."""

from __future__ import annotations

import re

from tests.unit_tests.test_surface_contrast import theme_colours
from tests.unit_tests.test_template_comments import REPO_ROOT

DOCS_CSS = REPO_ROOT / "docs" / "assets" / "extra.css"

DOCS_TOKEN = re.compile(r"--daiv-([\w-]+):\s*(#[0-9a-fA-F]{3,6})\s*;")


def docs_colours() -> dict[str, str]:
    return dict(DOCS_TOKEN.findall(DOCS_CSS.read_text(encoding="utf-8")))


def test_docs_theme_declares_its_colours():
    assert docs_colours()


def test_docs_theme_colours_match_the_app_theme():
    theme = {name.removeprefix("--color-"): value.lower() for name, value in theme_colours().items()}
    declared = {name: value.lower() for name, value in docs_colours().items()}

    assert {name: theme.get(name) for name in declared} == declared
