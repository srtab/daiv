"""Pins the list pages (Sessions, Artifacts, Memory) to the semantic design tokens.

DESIGN.md migrates a page whole, never half, so a legacy utility in one of these templates, or in a CSS class
they render, is a page drifting back. Status colours (dots, badges, merge request pills) match none of the
patterns, so they need no exemption.
"""

from __future__ import annotations

import re

import pytest

from tests.unit_tests.test_css_animations import DAIV_DIR, INPUT_CSS
from tests.unit_tests.test_surface_contrast import iter_rules

LEGACY = re.compile(r"(?:bg|border|text|ring|divide)-white/\[|text-gray-\d|text-white\b|bg-\[#")

SESSIONS = DAIV_DIR / "sessions" / "templates" / "sessions"
CORE = DAIV_DIR / "core" / "templates" / "core"
MEMORY = DAIV_DIR / "memory" / "templates" / "memory"

PAGE_TEMPLATES = {
    "sessions": [
        SESSIONS / "session_list.html",
        SESSIONS / "_filter_bar.html",
        SESSIONS / "_filter_dropdown.html",
        SESSIONS / "_filter_lead.html",
        SESSIONS / "_session_results.html",
        SESSIONS / "_session_row.html",
        CORE / "_pagination.html",
    ],
    "artifacts": [
        SESSIONS / "artifact_list.html",
        SESSIONS / "_artifact_results.html",
        SESSIONS / "_artifact_row.html",
    ],
    "memory": [MEMORY / "list.html", MEMORY / "_list_results.html"],
}

PAGE_CLASSES = {
    "sessions": [
        "filter-search",
        "filter-control",
        "filter-control--active",
        "filter-control__toggle",
        "filter-control__lead",
        "filter-control__key",
        "filter-control__value",
        "filter-control__clear",
        "filter-menu",
        "filter-menu--end",
        "filter-menu-item",
        "filter-menu-item--active",
        "filter-menu-item__lead",
        "filter-menu__section",
        "filter-menu__label",
        "filter-date",
        "filter-apply",
        "filter-chip",
        "filter-chip__x",
        "filter-clear",
        "filter-context",
        "filter-context__icon",
        "filter-context__text",
        "filter-context__clear",
        "session-group-header",
        "session-row",
        "session-repo",
        "session-branch",
        "session-num",
        "meta-pill",
        "empty-state",
        "empty-state__icon",
        "empty-state__title",
        "empty-state__body",
    ],
    "artifacts": ["artifact-tile", "artifact-download"],
    "memory": ["notice-found"],
}

RETIRED_CLASSES = ("filter-seg", "filter-seg-item", "filter-seg-item--active")


def _rules_for(class_name: str) -> list[str]:
    selector = re.compile(rf"\.{re.escape(class_name)}(?![\w-])")
    return [body for rule, body in iter_rules(INPUT_CSS) if selector.search(rule)]


@pytest.mark.parametrize("page", sorted(PAGE_TEMPLATES))
def test_list_page_templates_use_only_tokens(page):
    offenders = [
        f"{path.relative_to(DAIV_DIR)}: {match.group(0)}"
        for path in PAGE_TEMPLATES[page]
        for match in LEGACY.finditer(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "Legacy utilities on a token page:\n" + "\n".join(offenders)


@pytest.mark.parametrize("page", sorted(PAGE_CLASSES))
def test_list_page_classes_use_only_tokens(page):
    missing = [name for name in PAGE_CLASSES[page] if not _rules_for(name)]
    offenders = [
        f".{name}: {match.group(0)}"
        for name in PAGE_CLASSES[page]
        for body in _rules_for(name)
        for match in LEGACY.finditer(body)
    ]

    assert not missing, f"No CSS rule for: {missing}"
    assert not offenders, "Legacy utilities in a token page's classes:\n" + "\n".join(offenders)


def test_the_retired_segment_classes_are_gone():
    used = [
        f"{path.relative_to(DAIV_DIR)}: {name}"
        for path in DAIV_DIR.rglob("*.html")
        for name in RETIRED_CLASSES
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", path.read_text(encoding="utf-8"))
    ]
    defined = [name for name in RETIRED_CLASSES if _rules_for(name)]

    assert not used, used
    assert not defined, defined
