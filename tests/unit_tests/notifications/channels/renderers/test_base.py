from __future__ import annotations

import pytest
from notifications.channels.renderers.base import (
    REPO_BREAKDOWN_LIMIT,
    TONE_EMOJI,
    Row,
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

    def test_usage_renders_when_only_one_side_present(self):
        assert usage_rows({"input_tokens": 1000, "output_tokens": None}) == [Row("Usage", "1.0k in · — out")]

    def test_cost_row_skipped_when_cost_missing(self):
        assert usage_rows({"input_tokens": 1000}) == [Row("Usage", "1.0k in · — out")]


class TestSharedRepoList:
    def test_caps_at_the_shared_limit_with_an_overflow_marker(self):
        repos = [f"acme/r{i}" for i in range(REPO_BREAKDOWN_LIMIT + 4)]
        rendered = repo_list(repos)
        assert "and 4 more" in rendered
        assert f"acme/r{REPO_BREAKDOWN_LIMIT}" not in rendered

    def test_empty_input_renders_empty(self):
        assert repo_list([]) == ""
