from unittest.mock import AsyncMock, MagicMock, patch

from automation.agent.publishers import PublishOutcome
from codebase.base import MergeRequest, User
from codebase.managers.base import BaseManager


def _mr() -> MergeRequest:
    return MergeRequest(
        repo_id="g/r",
        merge_request_id=7,
        source_branch="daiv/feature",
        target_branch="main",
        title="t",
        description="d",
        author=User(id=1, username="daiv"),
        draft=True,
        web_url="https://example.com/mr/7",
    )


def _manager() -> BaseManager:
    ctx = MagicMock()
    ctx.sandbox = None
    with patch("codebase.managers.base.RepoClient.create_instance"):
        return BaseManager(runtime_ctx=ctx)


def _agent(state_values: dict) -> MagicMock:
    agent = MagicMock()
    agent.aget_state = AsyncMock(return_value=MagicMock(values=state_values))
    agent.aupdate_state = AsyncMock()
    return agent


class TestRecoverDraftPendingBranch:
    """Draft recovery runs right after an agent error — the moment most likely to hit the platform's
    branch-visibility lag — and it is the *second* publish() call site, so it has to honour the same
    pending-branch contract as the middleware or the feature has a hole exactly where it matters."""

    async def test_hands_the_owed_branch_back_to_the_publisher(self):
        """Recovery must reuse the owed branch, not mint a fresh one beside it.

        Without this, a recovery publish on a thread that already owes an MR generates another unique
        branch — the duplicate-branch amplification the pending state exists to prevent, in the code
        path most likely to run immediately after the first failure.
        """
        manager = _manager()
        agent = _agent({"merge_request": None, "pending_mr_branch": "daiv/owed"})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        assert pub_cls.return_value.publish.await_args.kwargs["pending_branch"] == "daiv/owed"

    async def test_persists_a_pending_branch_so_the_work_is_not_orphaned(self):
        """A pending outcome must be checkpointed, not dropped on the floor.

        Otherwise recovery reports "could not publish" while the work sits pushed on a branch nothing
        names: the state has no record, so the next turn starts another branch, and the user's only
        pointer to their changes never reaches them.
        """
        manager = _manager()
        agent = _agent({"merge_request": None})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(
                    merge_request=None, published=True, pending_branch="daiv/pushed", pending_branch_verified=True
                )
            )
            recovery = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        agent.aupdate_state.assert_awaited_once()
        values = agent.aupdate_state.await_args.kwargs["values"]
        assert values["pending_mr_branch"] == "daiv/pushed"
        assert values["pending_mr_branch_verified"] is True
        assert values["code_changes"] is True
        # The work reached the remote, so recovery did publish something — the caller's note must not
        # claim nothing was published.
        assert recovery.pending_branch == "daiv/pushed"
        assert recovery.published is True

    async def test_clears_a_stale_pending_branch_when_an_mr_is_recovered(self):
        """Recovering an MR settles the debt, so the pending branch must not linger in the checkpoint.

        This is the writer that could otherwise leave `merge_request` and `pending_mr_branch` set at
        once, making the reply link an MR while a notice claims one is still owed.
        """
        manager = _manager()
        agent = _agent({"merge_request": None, "pending_mr_branch": "daiv/owed"})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=_mr(), published=True))
            await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        values = agent.aupdate_state.await_args.kwargs["values"]
        assert values["merge_request"] is not None
        assert values["pending_mr_branch"] is None

    async def test_drops_the_state_mr_when_the_work_moved_to_a_pending_branch(self):
        """Recovery must mirror the middleware: a pending outcome invalidates the state MR.

        A pending branch is only reachable with an MR in state via the protected-branch fallback, i.e.
        the state MR is exactly the one DAIV could NOT push to. Leaving it checkpointed alongside the
        pending branch produces the combination ``PublishOutcome`` refuses to represent, and the two
        readers then disagree: the reply footer links the protected MR while the job result suppresses
        the pending notice entirely.
        """
        manager = _manager()
        agent = _agent({"merge_request": _mr(), "pending_mr_branch": None})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(
                    merge_request=None,
                    published=True,
                    pending_branch="daiv/replacement",
                    protected_branch_fallback_source="protected",
                )
            )
            await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        values = agent.aupdate_state.await_args.kwargs["values"]
        assert values["merge_request"] is None
        assert values["pending_mr_branch"] == "daiv/replacement"
        assert values["protected_branch_fallback_source"] == "protected"

    async def test_expires_a_debt_publish_says_is_no_longer_owed(self):
        """The middleware expires stale pending state on a nothing-published outcome; recovery must too,
        or the "your work is on branch X" notice repeats on every later turn in the thread."""
        manager = _manager()
        agent = _agent({"merge_request": None, "pending_mr_branch": "daiv/stale"})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            recovery = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        assert recovery.merge_request is None
        assert recovery.pending_branch is None
        values = agent.aupdate_state.await_args.kwargs["values"]
        assert values == {"pending_mr_branch": None, "pending_mr_branch_verified": False}

    async def test_keeps_the_work_when_only_the_checkpoint_write_fails(self):
        """A checkpointer blip must not be reported as "recovery failed" — the push already succeeded.

        With the state write inside the broad catch, a Redis hiccup discards the branch it was saving,
        returns False, and the user is told nothing was published while their commits sit on the remote.
        """
        manager = _manager()
        agent = _agent({"merge_request": None})
        agent.aupdate_state = AsyncMock(side_effect=RuntimeError("redis down"))

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(
                return_value=PublishOutcome(merge_request=None, published=True, pending_branch="daiv/pushed")
            )
            recovery = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        # The work reached the remote, so the caller must not claim otherwise.
        assert recovery.pending_branch == "daiv/pushed"

    async def test_reports_nothing_published_when_there_was_nothing(self):
        manager = _manager()
        agent = _agent({"merge_request": None})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(return_value=PublishOutcome(merge_request=None, published=False))
            recovery = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        assert recovery.merge_request is None
        assert recovery.pending_branch is None
        agent.aupdate_state.assert_not_called()

    async def test_swallows_publish_failure(self):
        """Recovery is best-effort: a failure here must not replace the original agent error."""
        manager = _manager()
        agent = _agent({"merge_request": None})

        with patch("codebase.managers.base.GitChangePublisher") as pub_cls:
            pub_cls.return_value.publish = AsyncMock(side_effect=RuntimeError("boom"))
            recovery = await manager._recover_draft(agent, {}, entity_label="issue", entity_id=1)

        assert recovery.merge_request is None
        assert recovery.pending_branch is None
