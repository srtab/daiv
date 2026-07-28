import json
import logging
from typing import TYPE_CHECKING, Any

from langgraph.store.memory import InMemoryStore
from redis.exceptions import RedisError

from automation.agent.middlewares.file_system import SandboxFileBackend
from automation.agent.publishers import GitChangePublisher, PublishOutcome
from automation.agent.results import (
    NO_SNAPSHOT,
    AgentResult,
    append_footer,
    build_agent_result,
    render_pending_branch_notice,
)
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from core.sandbox.client import get_run_sandbox_client

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.managers")


class BaseManager:
    """
    Base class for all managers.
    """

    _comment_id: str | None = None
    """ The comment ID where DAIV comments are stored. """

    _unable_note_posted: bool = False
    """ Backing flag for :meth:`_claim_unable_note`; see that method for the rationale. """

    def __init__(self, *, runtime_ctx: RuntimeCtx):
        self.ctx = runtime_ctx
        self.client = RepoClient.create_instance()
        self.store = InMemoryStore()

    def _claim_unable_note(self) -> bool:
        """Idempotency guard for the "unable to address" note.

        The note is posted from two stacked handlers: an inner one (wrapping the agent
        invocation, with draft-aware context) that re-raises, and a catch-all in the
        ``address_*`` entry point that also fires for failures *before* the agent ran.
        When the agent invocation itself fails, both would post — so the note method calls
        this first. Returns ``True`` the first time (caller posts the note, the inner
        draft-aware one winning) and ``False`` on every later call (caller skips).
        """
        if self._unable_note_posted:
            return False
        self._unable_note_posted = True
        return True

    async def _safe_get_state(self, agent: CompiledAgent, config: RunnableConfig, *, entity: str):
        """Read agent state, returning ``None`` on transport/serialization failure.

        Shared by every manager that renders run state into its reply: a checkpointer hiccup must cost
        the footers, never the reply itself.
        """
        try:
            return await agent.aget_state(config=config)
        except RedisError, OSError, json.JSONDecodeError:
            logger.warning("Failed to read agent state for %s", entity, exc_info=True)
            return None

    def _render_pending_footer(self, snapshot: Any) -> str | None:
        """The owed-merge-request notice for this platform, or ``None``."""
        return render_pending_branch_notice(snapshot, is_gitlab=self.ctx.git_platform == GitPlatform.GITLAB)

    def _render_footers(self, snapshot: Any) -> str | None:
        """Every "where did your work go" notice this run owes the reader, or ``None``.

        The reply posted to the issue/MR is built from the agent's own message, so notices derived from
        run state have to be composed in here — the job result's copy of the reply never reaches the
        person who triggered the run.

        Subclasses extend the set rather than the join: see
        :meth:`CommentsAddressorManager._render_footers`, which prepends the protected-branch footer.
        """
        return self._render_pending_footer(snapshot)

    @staticmethod
    def _append_footer(body: str, footer: str | None) -> str:
        """Attach a footer below a reply — see :func:`~automation.agent.results.append_footer`.

        Delegates so the Markdown setext rule has exactly one encoding, shared with the job-result path.
        """
        return append_footer(body, footer)

    async def _recover_draft(
        self, agent: CompiledAgent, config: RunnableConfig, *, entity_label: str, entity_id: int | str
    ) -> PublishOutcome:
        """
        Attempt to publish a draft MR from the agent's persisted state after an unexpected error.

        This is the second ``publish()`` call site (the first is ``GitMiddleware.aafter_agent``), so it
        has to honour the same pending-branch contract: reuse the branch an earlier turn already owes an
        MR for, and checkpoint one this attempt ends up owing. Dropping either turns a recovered turn
        into an orphaned branch plus a "could not publish" note. Both writers share
        :meth:`PublishOutcome.state_update` for exactly that reason.

        Returns:
            The publisher's own outcome. Deliberately not a bool: an opened merge request and a pushed
            branch that still owes one are both "work was saved", but callers must phrase them
            differently — telling someone a draft merge request exists when only a branch does sends
            them looking for something that isn't there. Returned unprojected so a field added to
            :class:`PublishOutcome` reaches these callers without a second type to update by hand.
        """
        try:
            snapshot = await agent.aget_state(config=config)
            snapshot_mr = snapshot.values.get("merge_request")

            # Sandbox-mode publish runs git through the run's bound backend. Recovery runs in the same
            # run scope as the agent (client still open), but doesn't hold the agent's backend instance,
            # so it reconstructs the bound handle from the run-scoped client + the persisted session id.
            sandbox_backend = None
            if self.ctx.sandbox is not None and self.ctx.sandbox.enabled and (sid := snapshot.values.get("session_id")):
                sandbox_backend = SandboxFileBackend(client=get_run_sandbox_client())
                sandbox_backend.bind_session(sid)

            publisher = GitChangePublisher(self.ctx, sandbox_backend=sandbox_backend)
            outcome = await publisher.publish(
                merge_request=snapshot_mr,
                as_draft=(snapshot_mr is None or snapshot_mr.draft),
                pending_branch=snapshot.values.get("pending_mr_branch"),
            )

        except Exception:
            logger.exception("Recovery failed after agent error for %s %s", entity_label, entity_id)
            return PublishOutcome(merge_request=None, published=False)

        # Deliberately outside the catch above: the push has already happened, so a checkpoint failure
        # here must not be reported as "recovery failed" — that would tell the user nothing was saved
        # while their commits sit on the remote.
        update_values = outcome.state_update(had_pending_branch=bool(snapshot.values.get("pending_mr_branch")))
        if update_values:
            try:
                await agent.aupdate_state(config=config, values=update_values)
            except Exception:
                logger.exception(
                    "Recovery published for %s %s but could not checkpoint it (merge_request=%s, "
                    "pending_branch=%s); the work is on the remote but the next turn will not know.",
                    entity_label,
                    entity_id,
                    outcome.merge_request.merge_request_id if outcome.merge_request else None,
                    outcome.pending_branch,
                )
        return outcome

    async def _build_agent_result(
        self,
        agent: CompiledAgent,
        config: RunnableConfig,
        *,
        response: str,
        usage: dict[str, Any] | None = None,
        snapshot: Any = NO_SNAPSHOT,
    ) -> AgentResult:
        """
        Build a standardized :class:`AgentResult` from the agent's persisted state.

        ``code_changes`` is a PrivateStateAttr, so it's omitted from ainvoke output.
        We read it from the persisted checkpoint instead. Pass ``snapshot`` to
        reuse a pre-fetched state and skip the extra Redis read; pass ``None``
        explicitly to signal the read already failed (no retry).

        An instance method rather than a static one so the run's platform reaches the owed-branch
        notice's merge-request/pull-request wording.
        """
        return await build_agent_result(
            agent,
            config,
            response=response,
            usage=usage,
            snapshot=snapshot,
            is_gitlab=self.ctx.git_platform == GitPlatform.GITLAB,
        )
