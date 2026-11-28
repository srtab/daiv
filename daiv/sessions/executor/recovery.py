import logging
from typing import TYPE_CHECKING, Any

from automation.agent.git_state import checkpointed_merge_request, effective_merge_request

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph

    from automation.agent.agent_settings import AgentSettings
    from automation.agent.context import RuntimeCtx
    from automation.agent.git_state import RepoPublish
    from automation.agent.publishers import PublishOutcome
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
    on_publish: Callable[[dict[str, Any]], None] | None = None,
) -> PublishOutcome | None:
    """Publish a draft merge request from the agent's checkpoint after the agent raised; return the main repository's
    outcome that landed a merge request, or ``None`` when none did.

    Runs inside the run's context, so the clone (and, on a sandbox run, its session) is still open. It publishes through
    ``workspace``, the one the agent worked in, with the run's own ``settings`` and ``session_links``, and recovers
    nothing when that workspace never became ready: the run acquired no sandbox session. Never raises: this is the last
    attempt to save the run's work, and a failure only means no draft. A merge left with conflict markers is not a
    draft to save.

    The main repository's submodules are recovered before it, which records the commits they pushed, and the
    session's extra repositories after it, each after its own submodules. Each lands on its own merge request recorded
    in the checkpoint's ``repo_publishes`` and handed to ``on_publish`` as a ``publish_result`` payload, and a failure
    on any side never stops the others.
    """
    from automation.agent.publishers import mark_recorded

    if not workspace.is_ready:
        logger.info("executor: no draft to recover for thread_id=%s: the run acquired no sandbox session", thread_id)
        return None
    main_submodules, record = await _recover_submodule_drafts(
        ctx, thread_id=thread_id, workspace=workspace, settings=settings, session_links=session_links
    )
    outcome, published = await _recover_main_draft(
        ctx,
        agent,
        config,
        thread_id=thread_id,
        workspace=workspace,
        settings=settings,
        session_links=session_links,
        record=record,
    )
    publishes = mark_recorded(main_submodules, record, recorded=published)
    if ctx.extra_repos:
        publishes += await _recover_extra_repo_drafts(
            ctx, thread_id=thread_id, workspace=workspace, settings=settings, session_links=session_links
        )
    if publishes:
        await _record_repo_drafts(agent, config, publishes, thread_id=thread_id, on_publish=on_publish)
    return outcome


async def _recover_submodule_drafts(
    ctx: RuntimeCtx,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
) -> tuple[list[RepoPublish], dict[str, str]]:
    """The main repository's submodules, published as drafts before it, and the commits it records for them."""
    from automation.agent.publishers import apublish_submodules

    try:
        return await apublish_submodules(
            ctx, workspace, ctx.repo, settings=settings, session_links=session_links, recovering=True
        )
    except Exception:
        logger.exception("executor: submodule recovery failed for thread_id=%s", thread_id)
        return [], {}


async def _recover_main_draft(
    ctx: RuntimeCtx,
    agent: CompiledStateGraph[Any, RuntimeCtx],
    config: RunnableConfig,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
    record: Mapping[str, str],
) -> tuple[PublishOutcome | None, bool]:
    """The main repository's outcome when it landed a merge request, and whether its publish went through, which
    decides whether the submodule commits it records count as recorded."""
    from automation.agent.publishers import GitChangePublisher, UnresolvedMergeConflictsError
    from automation.agent.workspace.git_manager import PublishScope

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
            merge_request=snapshot_mr,
            as_draft=(snapshot_mr is None or snapshot_mr.draft),
            scope=PublishScope.from_config(ctx.config.checkout, record=record),
        )

        if outcome.merge_request is not None:
            update_values: dict[str, Any] = {"merge_request": outcome.merge_request}
            if outcome.protected_branch_fallback_source:
                update_values["protected_branch_fallback_source"] = outcome.protected_branch_fallback_source
            await agent.aupdate_state(config=config, values=update_values)
            return outcome, True
        return None, True
    except UnresolvedMergeConflictsError as exc:
        logger.info("executor: no draft recovered for thread_id=%s: %s", thread_id, exc)
    except Exception:
        logger.exception("executor: draft recovery failed after an agent error for thread_id=%s", thread_id)

    return None, False


async def _recover_extra_repo_drafts(
    ctx: RuntimeCtx,
    *,
    thread_id: str,
    workspace: Workspace,
    settings: AgentSettings,
    session_links: SessionLinksResolver | None,
) -> list[RepoPublish]:
    from automation.agent.publishers import apublish_extra_repos

    try:
        return await apublish_extra_repos(
            ctx, workspace, settings=settings, session_links=session_links, recovering=True
        )
    except Exception:
        logger.exception("executor: extra repository recovery failed for thread_id=%s", thread_id)
        return []


async def _record_repo_drafts(
    agent: CompiledStateGraph[Any, RuntimeCtx],
    config: RunnableConfig,
    publishes: list[RepoPublish],
    *,
    thread_id: str,
    on_publish: Callable[[dict[str, Any]], None] | None,
) -> None:
    from automation.agent.events import repo_publish_payload

    if on_publish is not None:
        for publish in publishes:
            if (payload := repo_publish_payload(publish)) is not None:
                on_publish(payload)
    try:
        await agent.aupdate_state(config=config, values={"repo_publishes": [p.to_state() for p in publishes]})
    except Exception:
        logger.exception("executor: failed to record the other repositories' drafts for thread_id=%s", thread_id)
