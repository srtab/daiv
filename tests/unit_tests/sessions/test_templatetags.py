"""Tests for the sessions ``session_tags`` template filters.

Restores coverage lost with the deleted activity/test_templatetags.py and adds the
new ``duration`` / ``status_variant`` filters.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.staticfiles import finders
from django.utils import timezone

import pytest
from sessions.artifacts import ArtifactKind
from sessions.models import MergeRequestState
from sessions.templatetags.session_tags import (
    artifact_icon,
    date_bucket,
    duration,
    format_cost,
    format_tokens,
    linked_merge_request,
    origin_icon,
    session_merge_request,
    session_title,
    short_timestamp,
    status_variant,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (-5, ""),
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (7325, "2h 2m"),
        (90.9, "1m 30s"),  # floats are truncated to whole seconds
    ],
)
def test_duration(value, expected):
    assert duration(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (Decimal("0.0001"), "$0.0001"),  # sub-cent -> 4 decimals
        (Decimal("0.009"), "$0.0090"),
        (Decimal("0.01"), "$0.01"),  # cent boundary -> 2 decimals
        (Decimal("1.239"), "$1.24"),
        ("0.5", "$0.50"),  # string coercion
    ],
)
def test_format_cost(value, expected):
    assert format_cost(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, ""), (999, "999"), (1000, "1.0k"), (45300, "45.3k"), (1_000_000, "1.0M"), (2_500_000, "2.5M")],
)
def test_format_tokens(value, expected):
    assert format_tokens(value) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("SUCCESSFUL", "success"),
        ("FAILED", "failed"),
        ("RUNNING", "running"),
        ("QUEUED", "queued"),
        ("WAITING_INPUT", "waiting"),
        ("READY", "pending"),  # unmapped -> pending
        ("anything-else", "pending"),
    ],
)
def test_status_variant(status, expected):
    assert status_variant(status) == expected


def test_session_title_prefers_stored_title():
    session = types.SimpleNamespace(title="  Fix the bug  ", thread_id="abcdef123456")
    assert session_title(session) == "Fix the bug"


def test_session_title_falls_back_to_thread_prefix():
    session = types.SimpleNamespace(title="", thread_id="abcdef123456")
    assert session_title(session) == "abcdef12"


class _Runs:
    """Minimal stand-in for session.runs.all() over a fixed list."""

    def __init__(self, runs):
        self._runs = runs

    def all(self):
        return self._runs


def test_date_bucket_today_and_yesterday():
    now = timezone.localtime(timezone.now())
    assert date_bucket(now) == "Today"
    assert date_bucket(now - timedelta(days=1)) == "Yesterday"


def test_date_bucket_week_and_month_windows():
    now = timezone.localtime(timezone.now())
    assert date_bucket(now - timedelta(days=4)) == "Previous 7 days"
    assert date_bucket(now - timedelta(days=15)) == "Previous 30 days"


def test_date_bucket_older_returns_month_year():
    assert date_bucket(timezone.make_aware(datetime(2024, 3, 9, 12, 0))) == "March 2024"


def test_date_bucket_none_returns_earlier():
    assert date_bucket(None) == "Earlier"


def test_short_timestamp_is_relative_today_and_a_date_before():
    assert short_timestamp(timezone.now()) == "now"
    assert short_timestamp(timezone.make_aware(datetime(2024, 3, 9, 12, 0))) == "Mar 9"


@pytest.mark.parametrize("kind", [*ArtifactKind.values, "unknown-kind"])
def test_artifact_icon_resolves_to_a_shipped_icon(kind):
    assert finders.find(f"core/img/icons/{artifact_icon(kind)}.svg")


def test_date_bucket_exact_window_edges(monkeypatch):
    # Pin the <= 7 / <= 30 cutoffs at their exact edge (interior points are covered above).
    # Freeze "now" so days lands on exactly 7 / 30 with no midnight-crossing race.
    from sessions.templatetags import session_tags

    fixed = timezone.make_aware(datetime(2026, 6, 15, 12, 0))
    monkeypatch.setattr(session_tags.timezone, "now", lambda: fixed)
    assert date_bucket(fixed - timedelta(days=7)) == "Previous 7 days"
    assert date_bucket(fixed - timedelta(days=30)) == "Previous 30 days"


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("chat", "chat-bubble"),
        ("api_job", "command-line"),
        ("mcp_job", "cube"),
        ("schedule", "clock"),
        ("ui_job", "bolt"),
        ("issue_webhook", "exclamation-circle"),
        ("mr_webhook", "merge-request"),
        ("delegated_job", "agent"),
        ("delegation_resume", "arrow-path"),
        ("unknown", "jobs"),
    ],
)
def test_origin_icon(origin, expected):
    assert origin_icon(origin) == expected


def _mr_run(iid=None, url=""):
    return types.SimpleNamespace(merge_request_iid=iid, merge_request_web_url=url)


@pytest.mark.parametrize(
    ("scoped_iid", "runs", "expected"),
    [
        (
            None,
            [_mr_run(), _mr_run(12, "https://git/mr/12"), _mr_run(9, "https://git/mr/9")],
            (12, "https://git/mr/12"),
        ),
        (5, [_mr_run(12, "https://git/mr/12"), _mr_run(5, "https://git/mr/5")], (5, "https://git/mr/5")),
        (5, [_mr_run(5), _mr_run(5, "https://git/mr/5")], (5, "https://git/mr/5")),
        (5, [_mr_run(12, "https://git/mr/12")], (5, "")),
        (5, [_mr_run()], (5, "")),
    ],
    ids=["newest-run-that-published", "session-scope-wins", "older-run-url", "never-another-mrs-url", "no-url"],
)
def test_linked_merge_request(scoped_iid, runs, expected):
    session = types.SimpleNamespace(merge_request_iid=scoped_iid, runs=_Runs(runs))
    assert linked_merge_request(session) == {"iid": expected[0], "url": expected[1]}


def test_session_merge_request_carries_the_attached_state():
    runs = _Runs([_mr_run(5, "https://git/mr/5")])
    session = types.SimpleNamespace(merge_request_iid=None, runs=runs, merge_request_state=MergeRequestState.MERGED)
    assert session_merge_request(session) == {"iid": 5, "url": "https://git/mr/5", "state": MergeRequestState.MERGED}


def test_session_merge_request_none_when_nothing_published():
    session = types.SimpleNamespace(merge_request_iid=None, runs=_Runs([_mr_run()]), merge_request_state=None)
    assert session_merge_request(session) is None


def _render_filter_query(rf, params: dict, key: str, value: str) -> dict:
    import html

    from django.http import QueryDict
    from django.template import Context, Template

    out = Template(f"{{% load session_tags %}}{{% filter_query '{key}' '{value}' %}}").render(
        Context({"request": rf.get("/s/", params)})
    )
    assert out.startswith("?")
    return QueryDict(html.unescape(out[1:])).dict()


def test_filter_query_sets_the_key_and_drops_the_page(rf):
    query = _render_filter_query(
        rf, {"scope": "all", "page": "3", "status": "FAILED"}, "spend_model", "openrouter:z-ai/glm-5.2"
    )

    assert query == {"scope": "all", "status": "FAILED", "spend_model": "openrouter:z-ai/glm-5.2"}


def test_filter_query_removes_the_key_for_an_empty_value(rf):
    assert _render_filter_query(rf, {"scope": "all", "status": "FAILED"}, "status", "") == {"scope": "all"}


def _dropdown(rf, current: str):
    from django.template.loader import render_to_string

    from bs4 import BeautifulSoup

    model = "openrouter:z-ai/glm-5.2"
    html = render_to_string(
        "sessions/_filter_dropdown.html",
        {
            "key": "spend_model",
            "label": "Model",
            "icon": "cpu-chip",
            "all_label": "Any model",
            "options": [(model, model)],
            "current": current,
            "lead": "",
            "align": "",
        },
        request=rf.get("/s/"),
    )
    return BeautifulSoup(html, "html.parser")


def test_a_set_dropdown_shows_its_value_and_a_clear_link(rf):
    soup = _dropdown(rf, "openrouter:z-ai/glm-5.2")

    value = soup.select_one(".filter-control--active .filter-control__value")
    assert value.get_text(strip=True) == "openrouter:z-ai/glm-5.2"
    assert soup.select_one(".filter-control__clear")["href"] == "?"
    menu = soup.select_one("[data-surface].filter-menu")
    assert [a["data-value"] for a in menu.select("[data-filter-key=spend_model]")] == ["", "openrouter:z-ai/glm-5.2"]


def test_an_unset_dropdown_shows_its_name_and_hides_its_clear(rf):
    soup = _dropdown(rf, "")

    assert soup.select(".filter-control--active") == []
    assert soup.select_one(".filter-control__toggle").get_text(" ", strip=True) == "Model"
    assert soup.select_one(".filter-control__clear")["style"] == "display:none"
