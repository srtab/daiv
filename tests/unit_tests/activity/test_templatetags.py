from decimal import Decimal

import pytest
from activity.models import Activity, TriggerType
from activity.templatetags.activity_tags import activity_title, format_cost, format_tokens


class TestActivityTitle:
    def _activity(self, **kwargs):
        # In-memory Activity (unsaved) is fine for pure formatting logic.
        defaults = {"trigger_type": TriggerType.UI_JOB, "repo_id": "acme/web", "prompt": ""}
        defaults.update(kwargs)
        return Activity(**defaults)

    def test_uses_prompt_first_line_when_present(self):
        activity = self._activity(prompt="Refactor checkout\nDetails: remove the redirect")
        assert activity_title(activity) == "Refactor checkout"

    def test_strips_leading_whitespace_and_blank_lines(self):
        activity = self._activity(prompt="\n   \n   Do the thing   \n")
        assert activity_title(activity) == "Do the thing"

    def test_truncates_long_single_line_to_100_chars_with_ellipsis(self):
        long = "x" * 250
        activity = self._activity(prompt=long)
        title = activity_title(activity)
        assert len(title) == 101  # 100 chars + ellipsis
        assert title.endswith("…")
        assert title.startswith("x" * 100)

    def test_issue_webhook_with_no_prompt_uses_issue_iid(self):
        activity = self._activity(prompt="", trigger_type=TriggerType.ISSUE_WEBHOOK, issue_iid=412)
        assert activity_title(activity) == "Issue #412"

    def test_issue_webhook_without_iid_falls_back_to_trigger_label(self):
        activity = self._activity(prompt="", trigger_type=TriggerType.ISSUE_WEBHOOK, issue_iid=None)
        assert activity_title(activity) == "Issue"

    def test_mr_webhook_with_no_prompt_uses_mr_iid(self):
        activity = self._activity(prompt="", trigger_type=TriggerType.MR_WEBHOOK, merge_request_iid=1289)
        assert activity_title(activity) == "MR/PR !1289"

    def test_mr_webhook_without_iid_falls_back_to_trigger_label(self):
        activity = self._activity(prompt="", trigger_type=TriggerType.MR_WEBHOOK, merge_request_iid=None)
        assert activity_title(activity) == "MR/PR"

    def test_job_with_empty_prompt_falls_back_to_trigger_and_repo(self):
        activity = self._activity(prompt="   ", trigger_type=TriggerType.SCHEDULE, repo_id="acme/api")
        assert activity_title(activity) == "Scheduled Run on acme/api"

    def test_prompt_wins_over_issue_iid(self):
        activity = self._activity(prompt="My specific request", trigger_type=TriggerType.ISSUE_WEBHOOK, issue_iid=99)
        assert activity_title(activity) == "My specific request"


class TestFormatCost:
    def test_none_returns_empty(self):
        assert format_cost(None) == ""

    def test_sub_cent_shows_four_decimals(self):
        assert format_cost(Decimal("0.003")) == "$0.0030"

    def test_above_cent_shows_two_decimals(self):
        assert format_cost(Decimal("1.50")) == "$1.50"

    def test_exact_cent_boundary(self):
        assert format_cost(Decimal("0.01")) == "$0.01"

    def test_string_input(self):
        assert format_cost("0.005") == "$0.0050"


class TestFormatTokens:
    def test_none_returns_empty(self):
        assert format_tokens(None) == ""

    def test_small_number(self):
        assert format_tokens(500) == "500"

    def test_thousands(self):
        assert format_tokens(1500) == "1.5k"

    def test_millions(self):
        assert format_tokens(2_500_000) == "2.5M"

    @pytest.mark.parametrize("value", [999, 0, 1])
    def test_below_thousand(self, value):
        assert format_tokens(value) == str(value)
