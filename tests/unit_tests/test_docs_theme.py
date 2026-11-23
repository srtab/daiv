"""The docs theme mirrors tokens from `input.css`, which owns them."""

from __future__ import annotations

import re

from tests.unit_tests.test_surface_contrast import theme_colours
from tests.unit_tests.test_template_comments import REPO_ROOT

DOCS_CSS = REPO_ROOT / "docs" / "assets" / "extra.css"
DOCS_MAIN_HTML = REPO_ROOT / "docs" / "overrides" / "main.html"

THEME_COLOR_META = re.compile(r'<meta name="theme-color" content="([^"]+)">')


def test_docs_theme_colours_match_the_app_theme():
    theme = theme_colours()
    declared = theme_colours(DOCS_CSS)

    assert declared
    assert {name: theme.get(name) for name in declared} == declared


def test_docs_theme_color_meta_is_the_ground_colour():
    meta = THEME_COLOR_META.search(DOCS_MAIN_HTML.read_text(encoding="utf-8"))

    assert meta is not None
    assert meta.group(1) == theme_colours()["--color-ground"]
