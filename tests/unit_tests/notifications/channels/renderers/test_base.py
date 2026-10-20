from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from notifications.channels.renderers.base import (
    REPO_BREAKDOWN_LIMIT,
    TONE_EMOJI,
    Row,
    breakdown_value,
    compose_plain_text,
    fmt_cost,
    fmt_duration,
    fmt_tokens,
    repo_list,
    tone_of,
    usage_rows,
)


class TestToneResolution:
    @pytest.mark.parametrize("tone", ["success", "failure", "warning"])
    def test_the_envelope_tone_is_passed_through(self, tone):
        assert tone_of({"status_tone": tone}) == tone

    def test_a_context_predating_the_tone_key_falls_back_to_is_successful(self):
        assert tone_of({"is_successful": True}) == "success"
        assert tone_of({"is_successful": False}) == "failure"

    def test_the_envelope_tone_beats_is_successful(self):
        # A found-issues run finishes successfully but its envelope tone is a warning.
        assert tone_of({"status_tone": "warning", "is_successful": True}) == "warning"

    def test_an_unknown_tone_falls_back_rather_than_raising(self):
        assert tone_of({"status_tone": "bogus", "is_successful": True}) == "success"

    def test_every_tone_has_an_emoji(self):
        assert set(TONE_EMOJI) == {"success", "failure", "warning"}


class TestSharedFormatHelpers:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, None), (0, "0"), (42, "42"), (999, "999"), (1000, "1.0k"), (12432, "12.4k"), (596891, "596.9k")],
    )
    def test_fmt_tokens(self, value, expected):
        assert fmt_tokens(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, None), (0, "$0.00"), (0.214321, "$0.21"), (1.5, "$1.50"), (12.345, "$12.35")]
    )
    def test_fmt_cost(self, value, expected):
        assert fmt_cost(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, "—"), (0, "0s"), (47, "47s"), (60, "1m 00s"), (84, "1m 24s"), (3600, "1h 00m"), (3725, "1h 02m")],
    )
    def test_fmt_duration(self, value, expected):
        assert fmt_duration(value) == expected


class TestUsageRows:
    def test_no_usage_row_when_both_tokens_missing(self):
        assert usage_rows({"cost_usd": 0.10}) == [Row("Cost", "$0.10")]

    def test_one_token_side_without_a_cost_gives_only_a_usage_row(self):
        assert usage_rows({"input_tokens": 1000}) == [Row("Usage", "1.0k in · — out")]


class TestBreakdownValue:
    def test_the_input_count_appears_only_when_some_run_is_waiting(self):
        ctx = {"found_count": 2, "needs_attention_count": 1, "failed_count": 1}
        assert breakdown_value(ctx) == "found 2 · needs 1 · failed 1"
        assert breakdown_value({**ctx, "needs_input_count": 2}) == "found 2 · needs 1 · failed 1 · input 2"


class TestSharedRepoList:
    def test_caps_at_the_shared_limit_with_an_overflow_marker(self):
        repos = [f"acme/r{i}" for i in range(REPO_BREAKDOWN_LIMIT + 4)]
        rendered = repo_list(repos)
        assert "and 4 more" in rendered
        assert f"acme/r{REPO_BREAKDOWN_LIMIT}" not in rendered

    def test_empty_input_renders_empty(self):
        assert repo_list([]) == ""


class TestComposePlainText:
    def _notif(self, subject, body, link_url=""):
        return SimpleNamespace(subject=subject, body=body, link_url=link_url)

    def test_without_link_url(self):
        assert compose_plain_text(self._notif("Subject", "Body")) == "Subject\n\nBody"

    def test_with_link_url_appends_absolute_url(self):
        with patch("notifications.channels.renderers.base.build_absolute_url", return_value="https://daiv.test/x/"):
            result = compose_plain_text(self._notif("Subject", "Body", "/x/"))
        assert result == "Subject\n\nBody\n\nhttps://daiv.test/x/"
