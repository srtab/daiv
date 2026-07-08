"""Tests for the sessions ``session_tags`` template filters.

Restores coverage lost with the deleted activity/test_templatetags.py and adds the
new ``duration`` / ``status_variant`` filters.
"""

from __future__ import annotations

import types
from decimal import Decimal

import pytest
from sessions.templatetags.session_tags import duration, format_cost, format_tokens, session_title, status_variant


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
