"""Tests for the sessions ``session_tags`` template filters.

Restores coverage lost with the deleted activity/test_templatetags.py and adds the
new ``duration`` / ``status_variant`` filters.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

import pytest
from sessions.templatetags.session_tags import (
    day_bucket,
    duration,
    format_cost,
    format_tokens,
    origin_icon,
    session_cost,
    session_title,
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


def _session_at(dt):
    return types.SimpleNamespace(last_active_at=dt)


def test_day_bucket_today_and_yesterday():
    now = timezone.localtime(timezone.now())
    assert day_bucket(_session_at(now)) == "Today"
    assert day_bucket(_session_at(now - timedelta(days=1))) == "Yesterday"


def test_day_bucket_week_and_month_windows():
    now = timezone.localtime(timezone.now())
    assert day_bucket(_session_at(now - timedelta(days=4))) == "Previous 7 days"
    assert day_bucket(_session_at(now - timedelta(days=15))) == "Previous 30 days"


def test_day_bucket_older_returns_month_year():
    dt = timezone.make_aware(datetime(2024, 3, 9, 12, 0))
    assert day_bucket(_session_at(dt)) == "March 2024"


def test_day_bucket_none_or_missing_returns_earlier():
    assert day_bucket(_session_at(None)) == "Earlier"
    assert day_bucket(types.SimpleNamespace()) == "Earlier"  # attribute absent entirely


def test_day_bucket_exact_window_edges(monkeypatch):
    # Pin the <= 7 / <= 30 cutoffs at their exact edge (interior points are covered above).
    # Freeze "now" so days lands on exactly 7 / 30 with no midnight-crossing race.
    from sessions.templatetags import session_tags

    fixed = timezone.make_aware(datetime(2026, 6, 15, 12, 0))
    monkeypatch.setattr(session_tags.timezone, "now", lambda: fixed)
    assert day_bucket(_session_at(fixed - timedelta(days=7))) == "Previous 7 days"
    assert day_bucket(_session_at(fixed - timedelta(days=30))) == "Previous 30 days"


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
        ("unknown", "jobs"),
    ],
)
def test_origin_icon(origin, expected):
    assert origin_icon(origin) == expected


def test_session_cost_sums_runs():
    runs = [types.SimpleNamespace(cost_usd=Decimal("0.30")), types.SimpleNamespace(cost_usd=Decimal("0.08"))]
    session = types.SimpleNamespace(runs=_Runs(runs))
    assert session_cost(session) == "$0.38"


def test_session_cost_empty_when_zero():
    session = types.SimpleNamespace(runs=_Runs([types.SimpleNamespace(cost_usd=None)]))
    assert session_cost(session) == ""


def test_session_cost_skips_null_costs():
    # A run with cost_usd=None must be skipped (the `is not None` guard), not coerce a TypeError.
    runs = [types.SimpleNamespace(cost_usd=None), types.SimpleNamespace(cost_usd=Decimal("0.30"))]
    session = types.SimpleNamespace(runs=_Runs(runs))
    assert session_cost(session) == "$0.30"
