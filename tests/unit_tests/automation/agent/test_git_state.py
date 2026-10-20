import pytest

from automation.agent.git_state import checkpointed_merge_request, effective_merge_request
from tests.unit_tests.automation.agent.conftest import make_merge_request


class TestEffectiveMergeRequest:
    """The single decision behind every publish target: ``GitMiddleware`` for the turn-end publish
    and ``sessions.executor.recovery.recover_draft`` for the post-crash draft."""

    def test_prefers_the_context_mr(self):
        """MR-scope runs clone the MR's own source branch, so the context MR is authoritative even
        when the clone's recorded ref is something else (a commit-pinned clone records a SHA)."""
        context_mr = make_merge_request(merge_request_id=1, source_branch="a")
        state_mr = make_merge_request(merge_request_id=2, source_branch="b")

        assert effective_merge_request(context_mr=context_mr, state_mr=state_mr, current_ref="b") is context_mr

    def test_uses_the_state_mr_while_the_workspace_is_on_its_branch(self):
        state_mr = make_merge_request(merge_request_id=2, source_branch="feat/x")

        assert effective_merge_request(context_mr=None, state_mr=state_mr, current_ref="feat/x") is state_mr

    def test_drops_a_state_mr_whose_branch_the_workspace_is_not_on(self):
        """The whole point: pushing this run's commit onto that branch makes it a sibling of the
        branch's tip, not a descendant — a non-fast-forward that rebases into a conflict."""
        state_mr = make_merge_request(merge_request_id=2, source_branch="feat/x")

        assert effective_merge_request(context_mr=None, state_mr=state_mr, current_ref="main") is None

    def test_none_when_there_is_no_mr_at_all(self):
        assert effective_merge_request(context_mr=None, state_mr=None, current_ref="main") is None


class TestCheckpointedMergeRequest:
    """One reader for ``state["merge_request"]``, two policies: the middleware fails the run loud,
    the crash-recovery path degrades rather than discard the work it exists to save."""

    def test_it_passes_a_revived_model_through(self):
        mr = make_merge_request(merge_request_id=1, source_branch="a")

        assert checkpointed_merge_request({"merge_request": mr}, strict=True) is mr
        assert checkpointed_merge_request({}, strict=True) is None

    def test_strict_raises_on_a_value_that_did_not_revive(self, caplog):
        """``DAIVRedisSerializer`` returns the raw ``lc:2`` envelope when reconstruction fails."""
        envelope = {"lc": 2, "type": "constructor", "id": ["codebase.base", "MergeRequest"], "kwargs": {}}

        with caplog.at_level("ERROR"), pytest.raises(TypeError, match="expected MergeRequest"):
            checkpointed_merge_request({"merge_request": envelope}, strict=True)

        assert "revived as dict" in caplog.text

    def test_lenient_drops_it_loudly_instead(self, caplog):
        with caplog.at_level("ERROR"):
            assert checkpointed_merge_request({"merge_request": {"source_branch": "a"}}, strict=False) is None

        assert "revived as dict" in caplog.text
