from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from automation.agent.results import AgentResult, build_agent_result, parse_agent_result


class TestParseAgentResult:
    """Tests for parse_agent_result handling of current and legacy return_value formats."""

    def test_new_dict_format(self):
        rv = {"response": "Here are the files...", "code_changes": True}
        assert parse_agent_result(rv) == AgentResult(
            response="Here are the files...",
            code_changes=True,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_new_dict_format_no_code_changes(self):
        rv = {"response": "Done", "code_changes": False}
        assert parse_agent_result(rv) == AgentResult(
            response="Done",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_legacy_dict_code_changes_only(self):
        """Old format returned by address_issue_task / address_mr_comments_task before this change."""
        rv = {"code_changes": True}
        assert parse_agent_result(rv) == AgentResult(
            response="", code_changes=True, merge_request_id=None, merge_request_web_url=None, usage=None, question=None
        )

    def test_legacy_dict_code_changes_false(self):
        rv = {"code_changes": False}
        assert parse_agent_result(rv) == AgentResult(
            response="",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_empty_dict(self):
        assert parse_agent_result({}) == AgentResult(
            response="",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_legacy_string(self):
        """Old format returned by run_job_task before this change."""
        assert parse_agent_result("some text") == AgentResult(
            response="some text",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_empty_string(self):
        assert parse_agent_result("") == AgentResult(
            response="",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    def test_none(self):
        """return_value is None for failed/in-progress tasks."""
        assert parse_agent_result(None) == AgentResult(
            response="",
            code_changes=False,
            merge_request_id=None,
            merge_request_web_url=None,
            usage=None,
            question=None,
        )

    @pytest.mark.parametrize("rv", [{"response": "", "code_changes": False}, {"response": "", "code_changes": True}])
    def test_empty_response_preserves_code_changes(self, rv):
        result = parse_agent_result(rv)
        assert result["response"] == ""
        assert result["code_changes"] == rv["code_changes"]

    def test_dict_with_extra_keys_ignored(self):
        """Extra keys in the dict (e.g. from record_merge_metrics_task) don't break parsing."""
        rv = {"recorded": True}
        result = parse_agent_result(rv)
        assert result["response"] == ""
        assert result["code_changes"] is False

    def test_merge_request_fields(self):
        rv = {
            "response": "Created MR",
            "code_changes": True,
            "merge_request_id": 42,
            "merge_request_web_url": "https://gitlab.example.com/repo/-/merge_requests/42",
        }
        result = parse_agent_result(rv)
        assert result["merge_request_id"] == 42
        assert result["merge_request_web_url"] == "https://gitlab.example.com/repo/-/merge_requests/42"

    def test_merge_request_fields_absent(self):
        """Legacy dicts without MR fields default to None."""
        rv = {"response": "Done", "code_changes": False}
        result = parse_agent_result(rv)
        assert result["merge_request_id"] is None
        assert result["merge_request_web_url"] is None


def test_parse_agent_result_keeps_the_question():
    parsed = parse_agent_result({"response": "r", "question": {"questions": []}})
    assert parsed["question"] == {"questions": []}


def test_parse_agent_result_defaults_the_question_for_older_results():
    assert parse_agent_result({"response": "r"})["question"] is None
    assert parse_agent_result("plain text")["question"] is None


class TestParseAgentResultUsageFields:
    """Verify parse_agent_result handles the new usage fields gracefully."""

    def test_result_with_usage(self):
        rv = {
            "response": "Done",
            "code_changes": False,
            "merge_request_id": None,
            "merge_request_web_url": None,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "cost_usd": "0.018",
                "by_model": {"claude-sonnet-4-6": {"input_tokens": 1000, "output_tokens": 500}},
            },
        }
        result = parse_agent_result(rv)
        assert result["response"] == "Done"
        assert result["usage"]["input_tokens"] == 1000
        assert result["usage"]["cost_usd"] == "0.018"

    def test_result_without_usage_backward_compat(self):
        """Old stored results without usage field parse cleanly."""
        rv = {"response": "Done", "code_changes": True}
        result = parse_agent_result(rv)
        assert result["usage"] is None

    def test_legacy_string_has_no_usage(self):
        result = parse_agent_result("some text")
        assert result["usage"] is None

    def test_none_has_no_usage(self):
        result = parse_agent_result(None)
        assert result["usage"] is None


class TestBuildAgentResult:
    async def test_a_merge_request_that_did_not_revive_is_dropped_loudly(self, caplog):
        envelope = {"lc": 2, "type": "constructor", "id": ["codebase.base", "MergeRequest"], "kwargs": {}}
        snapshot = SimpleNamespace(values={"merge_request": envelope, "code_changes": True})

        with caplog.at_level("ERROR"):
            result = await build_agent_result(MagicMock(), {}, response="done", snapshot=snapshot)

        assert (result["merge_request_id"], result["merge_request_web_url"]) == (None, None)
        assert result["code_changes"] is True
        assert "revived as dict" in caplog.text

    async def test_question_round_trips_into_the_result(self):
        snapshot = SimpleNamespace(values={})

        result = await build_agent_result(
            MagicMock(), {}, response="done", snapshot=snapshot, question={"questions": []}
        )

        assert result["question"] == {"questions": []}

    async def test_question_defaults_to_none_including_the_snapshot_none_branch(self):
        with_snapshot = await build_agent_result(MagicMock(), {}, response="done", snapshot=SimpleNamespace(values={}))
        without_snapshot = await build_agent_result(MagicMock(), {}, response="done", snapshot=None)

        assert with_snapshot["question"] is None
        assert without_snapshot["question"] is None
