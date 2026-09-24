"""``Session.ref`` is what carries an issue session's working branch across webhook turns.

The read side lives in ``address_issue_task`` (see ``tests/unit_tests/codebase/test_tasks.py``); this pins the
write side for issue runs — the executor moving the pointer onto the branch the run published to, on the same
terms as job runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage

from codebase.base import Issue, MergeRequest, User
from codebase.managers.issue_addressor import IssueAddressorManager
from tests.unit_tests.codebase.managers.conftest import addressor_agent, addressor_run

_AUTHOR = User(id=1, username="alice")


def _mr(*, source_branch: str) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=449,
        source_branch=source_branch,
        target_branch="master",
        title="t",
        description="d",
        web_url="https://git/owner/repo/-/merge_requests/449",
        author=User(id=2, username="daiv"),
    )


async def _run(*, published_mr: MergeRequest | None, cloned_ref: str, persist_raises: bool = False):
    """Drive an issue run to completion; returns the persist mock, the result and the agent."""
    agent = addressor_agent(
        return_value={"messages": [AIMessage(content="done")]},
        state_values={"merge_request": published_mr, "code_changes": published_mr is not None},
    )
    ctx = SimpleNamespace(config=MagicMock(), repo=SimpleNamespace(ref=cloned_ref))
    with addressor_run(agent, ctx=ctx) as run:
        if persist_raises:
            run.persist.side_effect = RuntimeError("db down")
        result = await IssueAddressorManager.address_issue(
            repo_id="owner/repo",
            issue=Issue(id=1, iid=10, title="t", author=_AUTHOR, labels=["daiv-auto"]),
            thread_id="t-1",
        )

    return run.persist, result, agent


class TestIssueAddressorPersistsSessionRef:
    async def test_the_published_branch_becomes_the_session_ref(self, stub_base_init):
        persist, _, agent = await _run(
            published_mr=_mr(source_branch="fix/10-update-dependencies"), cloned_ref="master"
        )

        assert persist.await_args.kwargs["thread_id"] == "t-1"
        assert persist.await_args.kwargs["current_ref"] == "master"
        assert persist.await_args.kwargs["merge_request"].source_branch == "fix/10-update-dependencies"
        # The snapshot this reads is threaded into the result, so the turn pays one checkpoint read, not two.
        agent.aget_state.assert_awaited_once()

    async def test_a_run_that_published_nothing_moves_no_pointer(self, stub_base_init):
        """``apersist_session_ref`` is a no-op on a ``None`` MR, but it must still be reached with it rather than
        skipped on a guess — the checkpoint is the authority on what published."""
        persist, _, _ = await _run(published_mr=None, cloned_ref="master")

        assert persist.await_args.kwargs["merge_request"] is None

    async def test_a_failed_pointer_write_does_not_fail_the_run(self, stub_base_init, caplog):
        """The agent finished and its work already landed, before the ref sync runs. A cosmetic pointer must never
        turn that into a failed run — the issue would get an "unexpected error" note for work that actually landed."""
        with caplog.at_level("ERROR"):
            persist, result, _ = await _run(
                published_mr=_mr(source_branch="fix/10"), cloned_ref="master", persist_raises=True
            )

        persist.assert_awaited_once()
        assert result["response"] == "done"
        assert "failed to persist session ref" in caplog.text
