import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from automation.agent.workspace.base import Workspace
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.sessions")


async def recover_draft(
    ctx: RuntimeCtx, agent: CompiledAgent, config: RunnableConfig, *, thread_id: str, workspace: Workspace
) -> bool:
    """Publish a draft merge request from the agent's checkpoint after the agent raised; return whether one landed.

    Runs inside the run's context, so the clone and the sandbox session are still open. It publishes through
    ``workspace``, the one the agent worked in, and recovers nothing when that workspace never became ready: the agent
    raised before acquiring its sandbox session. Never raises: this is the last attempt to save the run's work, and a
    failure only means no draft.
    """
    from automation.agent.publishers import GitChangePublisher, checkpointed_merge_request, effective_merge_request

    if not workspace.is_ready:
        logger.info(
            "executor: no draft to recover for thread_id=%s: the agent raised before its sandbox session was acquired",
            thread_id,
        )
        return False
    try:
        snapshot = await agent.aget_state(config=config)
        # ``strict=False``: raising here would land in the catch-all below and discard the work this saves.
        snapshot_mr = effective_merge_request(
            context_mr=ctx.merge_request,
            state_mr=checkpointed_merge_request(snapshot.values, strict=False),
            current_ref=ctx.repo.current_ref,
        )
        publisher = GitChangePublisher(ctx, workspace, thread_id=thread_id)
        outcome = await publisher.publish(
            merge_request=snapshot_mr, as_draft=(snapshot_mr is None or snapshot_mr.draft)
        )

        if outcome.merge_request is not None:
            update_values: dict[str, Any] = {"merge_request": outcome.merge_request}
            if outcome.protected_branch_fallback_source:
                update_values["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
            await agent.aupdate_state(config=config, values=update_values)
            return True
    except Exception:
        logger.exception("executor: draft recovery failed after an agent error for thread_id=%s", thread_id)

    return False
