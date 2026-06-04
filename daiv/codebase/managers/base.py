import logging
from typing import TYPE_CHECKING, Any

from langgraph.store.memory import InMemoryStore

from automation.agent.publishers import GitChangePublisher
from automation.agent.results import NO_SNAPSHOT, AgentResult, build_agent_result
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

    def __init__(self, *, runtime_ctx: RuntimeCtx):
        self.ctx = runtime_ctx
        self.client = RepoClient.create_instance()
        self.store = InMemoryStore()

    async def _recover_draft(
        self, agent: CompiledAgent, config: RunnableConfig, *, entity_label: str, entity_id: int | str
    ) -> bool:
        """
        Attempt to publish a draft MR from the agent's persisted state after an unexpected error.

        Returns:
            Whether a draft merge request was successfully published.
        """
        try:
            snapshot = await agent.aget_state(config=config)
            snapshot_mr = snapshot.values.get("merge_request")

            # Sandbox-mode publish runs git through the run-scoped client opened by set_runtime_ctx
            # (still active here — recovery runs in the same run scope as the agent). Local /
            # sandbox-disabled runs need no client.
            sandbox_client = (
                get_run_sandbox_client() if self.ctx.sandbox is not None and self.ctx.sandbox.enabled else None
            )
            publisher = GitChangePublisher(self.ctx, sandbox_client=sandbox_client)
            outcome = await publisher.publish(
                session_id=snapshot.values.get("session_id"),
                merge_request=snapshot_mr,
                as_draft=(snapshot_mr is None or snapshot_mr.draft),
            )

            if outcome.merge_request is not None:
                update_values: dict[str, Any] = {"merge_request": outcome.merge_request}
                if outcome.protected_branch_fallback_source:
                    update_values["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
                await agent.aupdate_state(config=config, values=update_values)
                return True
        except Exception:
            logger.exception("Recovery failed after agent error for %s %s", entity_label, entity_id)

        return False

    @staticmethod
    async def _build_agent_result(
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
        """
        return await build_agent_result(agent, config, response=response, usage=usage, snapshot=snapshot)
