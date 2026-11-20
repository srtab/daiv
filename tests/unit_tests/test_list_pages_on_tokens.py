"""Pins the list pages (Sessions, Artifacts, Memory) and Insights' Spend page to the semantic design tokens.

DESIGN.md migrates a page whole, never half, so a legacy utility or a raw palette colour in one of these templates,
or in a CSS class they render, is a page drifting back.
"""

from __future__ import annotations

import re

import pytest

from tests.unit_tests.test_css_animations import DAIV_DIR, INPUT_CSS
from tests.unit_tests.test_surface_contrast import iter_rules

PALETTE = "red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|gray"
LEGACY = re.compile(
    rf"(?:bg|border|text|ring|divide)-white/\[|(?:bg|border|text|ring|divide)-(?:{PALETTE})-\d|text-white\b"
    r"|bg-\[#|rgba?\(|shadow-\["
)

SESSIONS = DAIV_DIR / "sessions" / "templates" / "sessions"
CORE = DAIV_DIR / "core" / "templates" / "core"
MEMORY = DAIV_DIR / "memory" / "templates" / "memory"
DASHBOARD = DAIV_DIR / "dashboard" / "templates" / "dashboard"

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
    "spend": [
        DASHBOARD / "insights_spend.html",
        DASHBOARD / "_spend_breakdown.html",
        DASHBOARD / "_spend_breakdown_row.html",
        DASHBOARD / "_spend_split.html",
        DASHBOARD / "_chart_card.html",
        DASHBOARD / "_change_badge.html",
        DASHBOARD / "_change_pill.html",
        CORE / "_info_tip.html",
    ],
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
        "filter-range",
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
        "session-row--failed",
        "status-dot",
        "status-dot-success",
        "status-dot-failed",
        "status-dot-running",
        "status-dot-queued",
        "status-dot-waiting",
        "status-dot-pending",
        "mr-pill",
        "mr-pill--merged",
        "mr-pill--closed",
        "session-repo",
        "session-branch",
        "session-num",
        "session-dot-slot",
        "session-row__title",
        "session-row__meta",
        "session-age",
        "meta-pill",
        "empty-state",
        "empty-state__icon",
        "empty-state__title",
        "empty-state__body",
    ],
    "artifacts": ["artifact-tile", "artifact-download"],
    "memory": ["notice-found", "status-badge", "status-badge-success", "status-badge-pending"],
    "spend": [
        "btn-surface",
        "link",
        "stat-tile",
        "change-badge",
        "change-badge--good",
        "change-badge--bad",
        "change-badge--neutral",
        "cache-tag",
        "cache-tag--good",
        "cache-tag--fair",
        "cache-tag--attention",
        "split-bar",
        "chart-card",
        "info-tip__btn",
        "info-tip__panel",
    ],
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
