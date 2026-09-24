import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.sessions")


async def recover_draft(ctx: RuntimeCtx, agent: CompiledAgent, config: RunnableConfig, *, thread_id: str) -> bool:
    """Publish a draft merge request from the agent's checkpoint after the agent raised; return whether one landed.

    Runs inside the run's context, so the clone and the sandbox client are still open. Sandbox-mode publish runs
    git through a backend bound to the turn's session, rebuilt here from the persisted session id. Never raises:
    this is the last attempt to save the run's work, and a failure only means no draft.
    """
    from automation.agent.middlewares.file_system import SandboxFileBackend
    from automation.agent.publishers import GitChangePublisher, checkpointed_merge_request, effective_merge_request
    from codebase.utils import get_repo_ref
    from core.sandbox.client import get_run_sandbox_client

    try:
        snapshot = await agent.aget_state(config=config)
        # ``strict=False``: raising here would land in the catch-all below and discard the work this saves.
        snapshot_mr = effective_merge_request(
            context_mr=ctx.merge_request,
            state_mr=checkpointed_merge_request(snapshot.values, strict=False),
            current_ref=get_repo_ref(ctx.gitrepo),
        )

        sandbox_backend = None
        if ctx.sandbox is not None and ctx.sandbox.enabled and (sid := snapshot.values.get("session_id")):
            sandbox_backend = SandboxFileBackend(client=get_run_sandbox_client())
            sandbox_backend.bind_session(sid)

        publisher = GitChangePublisher(ctx, sandbox_backend=sandbox_backend, thread_id=thread_id)
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
