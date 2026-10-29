import logging
from typing import TYPE_CHECKING, Any

from automation.agent.git_state import checkpointed_merge_request, effective_merge_request

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph

    from automation.agent.agent_settings import AgentSettings
    from automation.agent.context import RuntimeCtx
    from automation.agent.session_links import SessionLinksResolver
    from automation.agent.workspace.base import Workspace

logger = logging.getLogger("daiv.sessions")


async def recover_draft(
    ctx: RuntimeCtx,
    agent: CompiledStateGraph[Any, RuntimeCtx],
    config: RunnableConfig,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
) -> bool:
    """Publish a draft merge request from the agent's checkpoint after the agent raised; return whether one landed.

    Runs inside the run's context, so the clone (and, on a sandbox run, its session) is still open. It publishes through
    ``workspace``, the one the agent worked in, with the run's own ``settings`` and ``session_links``, and recovers
    nothing when that workspace never became ready: the run acquired no sandbox session. Never raises: this is the last
    attempt to save the run's work, and a failure only means no draft. A merge left with conflict markers is not a
    draft to save.

    The session's extra repositories are recovered after the main one, each to its own merge request, and a failure on
    either side never stops the other; any merge request landing counts.
    """
    if not workspace.is_ready:
        logger.info("executor: no draft to recover for thread_id=%s: the run acquired no sandbox session", thread_id)
        return False
    landed = await _recover_main_draft(
        ctx, agent, config, thread_id=thread_id, workspace=workspace, settings=settings, session_links=session_links
    )
    if ctx.extra_repos and await _recover_extra_repo_drafts(
        ctx, agent, config, thread_id=thread_id, workspace=workspace, settings=settings, session_links=session_links
    ):
        landed = True
    return landed


async def _recover_main_draft(
    ctx: RuntimeCtx,
    agent: CompiledStateGraph[Any, RuntimeCtx],
    config: RunnableConfig,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
) -> bool:
    from automation.agent.publishers import GitChangePublisher, UnresolvedMergeConflictsError

    try:
        snapshot = await agent.aget_state(config=config)
        # ``strict=False``: raising here would land in the catch-all below and discard the work this saves.
        snapshot_mr = effective_merge_request(
            context_mr=ctx.merge_request,
            state_mr=checkpointed_merge_request(snapshot.values, strict=False),
            current_ref=ctx.repo.current_ref,
        )
        publisher = GitChangePublisher(ctx, workspace, settings=settings, session_links=session_links)
        outcome = await publisher.publish(
            merge_request=snapshot_mr, as_draft=(snapshot_mr is None or snapshot_mr.draft)
        )

        if outcome.merge_request is not None:
            update_values: dict[str, Any] = {"merge_request": outcome.merge_request}
            if outcome.protected_branch_fallback_source:
                update_values["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
            await agent.aupdate_state(config=config, values=update_values)
            return True
    except UnresolvedMergeConflictsError as exc:
        logger.info("executor: no draft recovered for thread_id=%s: %s", thread_id, exc)
    except Exception:
        logger.exception("executor: draft recovery failed after an agent error for thread_id=%s", thread_id)

    return False


async def _recover_extra_repo_drafts(
    ctx: RuntimeCtx,
    agent: CompiledStateGraph[Any, RuntimeCtx],
    config: RunnableConfig,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
) -> bool:
    from automation.agent.publishers import apublish_extra_repos

    try:
        publishes = await apublish_extra_repos(
            ctx, workspace, settings=settings, session_links=session_links, recovering=True
        )
    except Exception:
        logger.exception("executor: extra repository recovery failed for thread_id=%s", thread_id)
        return False
    try:
        await agent.aupdate_state(config=config, values={"repo_publishes": [p.to_state() for p in publishes]})
    except Exception:
        logger.exception("executor: failed to record the extra repositories' drafts for thread_id=%s", thread_id)
    return any(publish.merge_request is not None for publish in publishes)
