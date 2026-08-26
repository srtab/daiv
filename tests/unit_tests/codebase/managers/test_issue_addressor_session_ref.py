"""``Session.ref`` is what carries an issue session's working branch across webhook turns.

The read side lives in ``address_issue_task`` (see ``tests/unit_tests/codebase/test_tasks.py``);
this pins the write side — the issue addressor moving the pointer onto the branch it published
to, on the same terms ``run_job_task`` already does for job runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from codebase.base import GitPlatform, Issue, MergeRequest, Scope, User
from codebase.managers.issue_addressor import IssueAddressorManager
from codebase.repo_config import RepositoryConfig

_AUTHOR = User(id=1, username="alice")


def _ctx(*, cloned_ref: str) -> SimpleNamespace:
    """Only the attributes ``_address_issue`` and ``build_langsmith_config`` actually touch."""
    return SimpleNamespace(
        repository=SimpleNamespace(slug="owner/repo"),
        repo=SimpleNamespace(ref=cloned_ref),
        git_platform=GitPlatform.GITLAB,
        bot_username="daiv-bot",
        config=RepositoryConfig(),
        scope=Scope.ISSUE,
    )


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


async def _run(*, noop_checkpointer, published_mr: MergeRequest | None, cloned_ref: str, persist_raises: bool = False):
    """Drive ``_address_issue`` to completion; returns the persist mock, the result and the agent."""
    agent = MagicMock()
    agent.get_name.return_value = "DAIV Agent"
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="done")]})
    agent.aget_state = AsyncMock(
        return_value=SimpleNamespace(values={"merge_request": published_mr, "code_changes": published_mr is not None})
    )

    persist = AsyncMock(side_effect=RuntimeError("db down") if persist_raises else None)

    with (
        patch("codebase.managers.issue_addressor.open_checkpointer", noop_checkpointer),
        patch("codebase.managers.issue_addressor.create_daiv_agent", AsyncMock(return_value=agent)),
        patch("sessions.services.apersist_session_ref", persist),
        patch.object(IssueAddressorManager, "_leave_comment"),
    ):
        result = await IssueAddressorManager.address_issue(
            issue=Issue(id=1, iid=10, title="t", author=_AUTHOR, labels=["daiv-auto"]),
            runtime_ctx=_ctx(cloned_ref=cloned_ref),
            thread_id="t-1",
        )

    return persist, result, agent


class TestIssueAddressorPersistsSessionRef:
    async def test_the_published_branch_becomes_the_session_ref(self, stub_base_init, noop_checkpointer):
        persist, _, agent = await _run(
            noop_checkpointer=noop_checkpointer,
            published_mr=_mr(source_branch="fix/10-update-dependencies"),
            cloned_ref="master",
        )

        assert persist.await_args.kwargs["thread_id"] == "t-1"
        assert persist.await_args.kwargs["current_ref"] == "master"
        assert persist.await_args.kwargs["merge_request"].source_branch == "fix/10-update-dependencies"
        # The snapshot this reads is threaded into ``_build_agent_result``, so the turn pays one
        # checkpoint read rather than two.
        agent.aget_state.assert_awaited_once()

    async def test_a_run_that_published_nothing_moves_no_pointer(self, stub_base_init, noop_checkpointer):
        """``apersist_session_ref`` is a no-op on a ``None`` MR, but it must still be reached with
        it rather than skipped on a guess — the checkpoint is the authority on what published."""
        persist, _, _ = await _run(noop_checkpointer=noop_checkpointer, published_mr=None, cloned_ref="master")

        assert persist.await_args.kwargs["merge_request"] is None

    async def test_a_failed_pointer_write_does_not_fail_the_run(self, stub_base_init, noop_checkpointer, caplog):
        """The run already published and already answered the user. A cosmetic pointer must never
        turn that into a failed run — the issue would get an "unexpected error" note for work
        that actually landed."""
        with caplog.at_level("ERROR"):
            persist, result, _ = await _run(
                noop_checkpointer=noop_checkpointer,
                published_mr=_mr(source_branch="fix/10"),
                cloned_ref="master",
                persist_raises=True,
            )

        persist.assert_awaited_once()
        assert result["response"] == "done"
        assert "failed to persist session ref" in caplog.text
