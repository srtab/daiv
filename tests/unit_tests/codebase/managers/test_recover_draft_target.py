"""``_recover_draft`` is the second place a run hands a merge request to the publisher.

It exists to save the work of a run that crashed, so it is exactly where a stale publish target
costs the most: the turn-end publish has already failed, and this is the last attempt. It has to
apply the same guard ``GitMiddleware`` does — see ``automation.agent.publishers.
effective_merge_request``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

from codebase.base import Issue, MergeRequest, User
from codebase.managers.issue_addressor import IssueAddressorManager

_AUTHOR = User(id=1, username="alice")


def _ctx() -> Mock:
    ctx = Mock()
    ctx.repository = Mock(slug="owner/repo")
    ctx.sandbox = None
    ctx.merge_request = None
    ctx.gitrepo = Mock()
    return ctx


def _mr(*, source_branch: str) -> MergeRequest:
    return MergeRequest(
        repo_id="owner/repo",
        merge_request_id=449,
        source_branch=source_branch,
        target_branch="master",
        title="t",
        description="d",
        author=_AUTHOR,
    )


async def _recover(*, checkpointed_mr, current_ref: str):
    manager = IssueAddressorManager(
        issue=Issue(id=1, iid=10, title="t", author=_AUTHOR), runtime_ctx=_ctx(), thread_id="t-1"
    )
    agent = Mock()
    agent.aget_state = AsyncMock(return_value=Mock(values={"merge_request": checkpointed_mr, "session_id": None}))
    agent.aupdate_state = AsyncMock()

    with (
        patch("codebase.managers.base.GitChangePublisher") as pub_cls,
        patch("codebase.managers.base.get_repo_ref", return_value=current_ref),
    ):
        pub_cls.return_value.publish = AsyncMock(return_value=Mock(merge_request=None))
        await manager._recover_draft(agent, {}, entity_label="issue", entity_id=10)

    return pub_cls.return_value.publish.await_args.kwargs


class TestRecoverDraftPublishTarget:
    async def test_it_drops_a_checkpointed_mr_the_workspace_is_not_on(self, stub_base_init):
        """Re-publishing onto that branch is what already failed a moment ago — retrying it here
        just loses the work a second time. A fresh draft MR keeps it."""
        kwargs = await _recover(checkpointed_mr=_mr(source_branch="fix/10-update-dependencies"), current_ref="master")

        assert kwargs["merge_request"] is None
        # No MR to add onto means the draft has to be opened as a draft.
        assert kwargs["as_draft"] is True

    async def test_it_keeps_the_mr_when_the_workspace_is_on_its_branch(self, stub_base_init):
        """The ordinary crash-recovery case: the run was working on its own MR's branch, so the
        draft belongs on that MR rather than in a duplicate."""
        kwargs = await _recover(checkpointed_mr=_mr(source_branch="fix/10"), current_ref="fix/10")

        assert kwargs["merge_request"] is not None
        assert kwargs["merge_request"].merge_request_id == 449

    async def test_it_still_recovers_when_the_checkpoint_did_not_revive(self, stub_base_init, caplog):
        """``DAIVRedisSerializer`` hands back the raw envelope dict when a ``MergeRequest`` fails to
        reconstruct (a schema drift across a deploy). ``GitMiddleware`` raises on that, but raising
        *here* would land inside this method's own catch-all and discard the work it exists to save
        — so the dict is dropped, loudly, and the run still gets a fresh draft MR."""
        with caplog.at_level("ERROR"):
            kwargs = await _recover(checkpointed_mr={"source_branch": "fix/10"}, current_ref="fix/10")

        assert kwargs["merge_request"] is None
        assert kwargs["as_draft"] is True
        assert "revived as dict" in caplog.text
        # The catch-all never fired: it degraded rather than crashed.
        assert "Recovery failed" not in caplog.text
