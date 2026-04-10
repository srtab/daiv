import pytest

from automation.agent.results import AgentResult, parse_agent_result


class TestParseAgentResult:
    """Tests for parse_agent_result handling of current and legacy return_value formats."""

    def test_new_dict_format(self):
        rv = {"response": "Here are the files...", "code_changes": True}
        assert parse_agent_result(rv) == AgentResult(response="Here are the files...", code_changes=True)

    def test_new_dict_format_no_code_changes(self):
        rv = {"response": "Done", "code_changes": False}
        assert parse_agent_result(rv) == AgentResult(response="Done", code_changes=False)

    def test_legacy_dict_code_changes_only(self):
        """Old format returned by address_issue_task / address_mr_comments_task before this change."""
        rv = {"code_changes": True}
        assert parse_agent_result(rv) == AgentResult(response="", code_changes=True)

    def test_legacy_dict_code_changes_false(self):
        rv = {"code_changes": False}
        assert parse_agent_result(rv) == AgentResult(response="", code_changes=False)

    def test_empty_dict(self):
        assert parse_agent_result({}) == AgentResult(response="", code_changes=False)

    def test_legacy_string(self):
        """Old format returned by run_job_task before this change."""
        assert parse_agent_result("some text") == AgentResult(response="some text", code_changes=False)

    def test_empty_string(self):
        assert parse_agent_result("") == AgentResult(response="", code_changes=False)

    def test_none(self):
        """return_value is None for failed/in-progress tasks."""
        assert parse_agent_result(None) == AgentResult(response="", code_changes=False)

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
