from unittest.mock import AsyncMock, MagicMock

import pytest

from automation.agent.results import AgentResult, build_agent_result, parse_agent_result, render_pending_branch_notice


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
        )

    def test_new_dict_format_no_code_changes(self):
        rv = {"response": "Done", "code_changes": False}
        assert parse_agent_result(rv) == AgentResult(
            response="Done", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_legacy_dict_code_changes_only(self):
        """Old format returned by address_issue_task / address_mr_comments_task before this change."""
        rv = {"code_changes": True}
        assert parse_agent_result(rv) == AgentResult(
            response="", code_changes=True, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_legacy_dict_code_changes_false(self):
        rv = {"code_changes": False}
        assert parse_agent_result(rv) == AgentResult(
            response="", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_empty_dict(self):
        assert parse_agent_result({}) == AgentResult(
            response="", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_legacy_string(self):
        """Old format returned by run_job_task before this change."""
        assert parse_agent_result("some text") == AgentResult(
            response="some text", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_empty_string(self):
        assert parse_agent_result("") == AgentResult(
            response="", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
        )

    def test_none(self):
        """return_value is None for failed/in-progress tasks."""
        assert parse_agent_result(None) == AgentResult(
            response="", code_changes=False, merge_request_id=None, merge_request_web_url=None, usage=None
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


def _agent_with_state(values: dict) -> MagicMock:
    agent = MagicMock()
    agent.aget_state = AsyncMock(return_value=MagicMock(values=values))
    return agent


class TestBuildAgentResultPendingBranch:
    """A run that pushed but could not open an MR must say so.

    The result's only "where did the work go" channel is ``merge_request_web_url``, so without the
    notice the caller sees a null URL and reads it as "the agent changed nothing".
    """

    async def test_names_the_branch_when_no_mr_was_opened(self):
        agent = _agent_with_state({"code_changes": True, "merge_request": None, "pending_mr_branch": "daiv/feature"})

        result = await build_agent_result(agent, {}, response="Done.", is_gitlab=True)

        assert result["response"].startswith("Done.")
        assert "daiv/feature" in result["response"]
        assert result["code_changes"] is True
        assert result["merge_request_web_url"] is None

    async def test_stands_alone_when_the_agent_said_nothing(self):
        agent = _agent_with_state({"merge_request": None, "pending_mr_branch": "daiv/feature"})

        result = await build_agent_result(agent, {}, response="", is_gitlab=True)

        # No reply above it, so the notice must not open with the separator rule it normally needs.
        assert result["response"].startswith("####")
        assert "daiv/feature" in result["response"]

    async def test_an_absent_verified_flag_never_claims_the_work_is_safe(self):
        """Legacy checkpoints predate the flag, so the missing-key default is user-facing.

        It must round DOWN: defaulting a missing flag to "verified" would tell someone their changes are
        safely on a branch that was never confirmed to exist, which is precisely what stops them redoing
        work that never landed.
        """
        agent = _agent_with_state({"merge_request": None, "pending_mr_branch": "daiv/feature"})

        result = await build_agent_result(agent, {}, response="Done.", is_gitlab=True)

        assert "could not confirm" in result["response"]
        assert "Nothing is lost" not in result["response"]

    async def test_no_notice_without_a_pending_branch(self):
        agent = _agent_with_state({"code_changes": False, "merge_request": None})

        result = await build_agent_result(agent, {}, response="Nothing to change.", is_gitlab=True)

        assert result["response"] == "Nothing to change."

    async def test_no_notice_when_an_mr_exists(self):
        mr = MagicMock(merge_request_id=42, web_url="https://example.com/mr/42")
        agent = _agent_with_state({"code_changes": True, "merge_request": mr, "pending_mr_branch": "stale"})

        result = await build_agent_result(agent, {}, response="Done.", is_gitlab=True)

        assert result["response"] == "Done."
        assert result["merge_request_id"] == 42

    async def test_confirmed_branch_is_reported_as_holding_the_work(self):
        agent = _agent_with_state({
            "merge_request": None,
            "pending_mr_branch": "daiv/feature",
            "pending_mr_branch_verified": True,
        })

        result = await build_agent_result(agent, {}, response="Done.", is_gitlab=True)

        assert "daiv/feature" in result["response"]
        assert "Nothing is lost" in result["response"]

    async def test_unconfirmed_branch_does_not_claim_the_work_is_safe(self):
        """When the branch could not be confirmed, the notice must not say the changes are safe.

        Telling someone their work is on a branch is what stops them redoing it, so an assumption
        laundered into a certainty is the one direction this degradation must never round in.
        """
        agent = _agent_with_state({
            "merge_request": None,
            "pending_mr_branch": "daiv/feature",
            "pending_mr_branch_verified": False,
        })

        result = await build_agent_result(agent, {}, response="Done.", is_gitlab=True)

        assert "daiv/feature" in result["response"]
        assert "Nothing is lost" not in result["response"]
        assert "could not confirm" in result["response"]


class TestRenderPendingBranchNotice:
    """The notice has to be renderable outside build_agent_result.

    Issue- and MR-scope runs post their reply to the platform straight from the agent's message, so a
    notice that only exists inside the job's return value never reaches the person who triggered the
    run — the very surface where an MR-less run is indistinguishable from a run that did nothing.
    """

    def test_returns_none_without_a_pending_branch(self):

        assert render_pending_branch_notice(MagicMock(values={"merge_request": None}), is_gitlab=True) is None

    def test_returns_none_for_a_missing_snapshot(self):

        assert render_pending_branch_notice(None, is_gitlab=True) is None

    def test_names_the_branch(self):

        notice = render_pending_branch_notice(
            MagicMock(values={"pending_mr_branch": "daiv/x", "pending_mr_branch_verified": True}), is_gitlab=True
        )

        assert notice is not None
        assert "daiv/x" in notice

    @pytest.mark.parametrize(
        ("is_gitlab", "expected", "absent"),
        [
            pytest.param(True, "merge request", "pull request", id="gitlab"),
            pytest.param(False, "pull request", "merge request", id="github"),
        ],
    )
    def test_uses_the_platform_vocabulary(self, is_gitlab, expected, absent):
        """The notice names the thing the reader is looking for, and the two platforms call it different
        things — a hardcoded noun sends GitHub users hunting for a "merge request"."""

        notice = render_pending_branch_notice(
            MagicMock(values={"pending_mr_branch": "daiv/x", "pending_mr_branch_verified": True}), is_gitlab=is_gitlab
        )

        assert notice is not None
        assert expected in notice
        assert absent not in notice

    def test_explains_a_protected_source_branch_when_that_is_why_the_branch_exists(self):
        """The protected-branch footer stands down while the MR is owed (it links an MR that does not
        exist yet), so this notice has to carry that explanation or the reader is handed a branch with no
        account of why their own branch was not used."""

        notice = render_pending_branch_notice(
            MagicMock(
                values={
                    "pending_mr_branch": "daiv/replacement",
                    "pending_mr_branch_verified": True,
                    "protected_branch_fallback_source": "release/1.2",
                }
            ),
            is_gitlab=True,
        )

        assert notice is not None
        assert "release/1.2" in notice
        assert "protected" in notice


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
