import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from redis.exceptions import RedisError

from sessions.executor.lock import hold_session_lock
from sessions.executor.recovery import recover_draft
from sessions.executor.spec import RunHooks, RunOutcome
from sessions.models import Run, Session
from sessions.pipeline_watch.service import PipelineWatch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig
    from langgraph.types import StateSnapshot

    from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler
    from codebase.context import RuntimeCtx
    from sessions.executor.spec import RunSpec

logger = logging.getLogger("daiv.sessions")


@dataclass(frozen=True)
class AgentRun:
    """The built agent and what it runs with. ``usage`` tallies the run's tokens and cost, subagents included."""

    ctx: RuntimeCtx
    agent: CompiledAgent
    config: RunnableConfig
    usage: CostAwareUsageMetadataCallbackHandler


async def execute_run(spec: RunSpec, hooks: RunHooks | None = None) -> RunOutcome:
    """Run the agent once for ``spec``; the package docstring lists the order of the steps."""
    hooks = hooks or RunHooks()
    entered = False
    try:
        async with hold_session_lock(spec.lock, spec.thread_id):
            entered = True
            return await _run_in_slot(spec, hooks)
    except Exception as exc:
        if not entered:
            await _notify_failure(hooks, exc)
        raise


async def _run_in_slot(spec: RunSpec, hooks: RunHooks) -> RunOutcome:
    recovery = _Recovery()
    try:
        outcome = await _invoke(spec, hooks, recovery)
    except Exception as exc:
        await _notify_failure(hooks, exc, draft_published=recovery.draft_published, snapshot=recovery.snapshot)
        raise
    if hooks.on_success is not None:
        await hooks.on_success(outcome)
    return outcome


@dataclass
class _Recovery:
    """What draft recovery leaves for ``on_failure``: filled inside the run's context, read after it closes."""

    draft_published: bool = False
    snapshot: StateSnapshot | None = None


async def _invoke(spec: RunSpec, hooks: RunHooks, recovery: _Recovery) -> RunOutcome:
    from automation.agent.utils import extract_text_content

    async with _agent_run(spec, hooks) as run:
        try:
            result = await run.agent.ainvoke(
                {"messages": list(spec.input_messages)}, config=run.config, context=run.ctx
            )
        except Exception:
            await _recover(spec, run, recovery)
            raise
        messages = result.get("messages")
        if not messages:
            raise ValueError(f"Agent returned no messages for repo_id={spec.repo_id}")
        return await _after_run(spec, run, response_text=extract_text_content(messages[-1].content))


async def _recover(spec: RunSpec, run: AgentRun, recovery: _Recovery) -> None:
    """Publish a draft from the checkpoint after an agent error, if the spec asks for it, and re-read the checkpoint
    for ``on_failure``. Runs while the clone and the sandbox are still open."""
    if not spec.recover_draft:
        return
    recovery.draft_published = await recover_draft(run.ctx, run.agent, run.config, thread_id=spec.thread_id)
    recovery.snapshot = await _read_snapshot_after_recovery(run, spec.thread_id)


@asynccontextmanager
async def _agent_run(spec: RunSpec, hooks: RunHooks) -> AsyncIterator[AgentRun]:
    # Imported here so django.setup(), which reaches this module via jobs.tasks, never loads the agent stack.
    from automation.agent.graph import create_daiv_agent
    from automation.agent.usage_tracking import track_usage_metadata
    from automation.agent.utils import build_langsmith_config, get_daiv_agent_kwargs
    from codebase.context import set_runtime_ctx
    from core.checkpointer import open_checkpointer

    async with (
        set_runtime_ctx(
            repo_id=spec.repo_id,
            scope=spec.scope,
            ref=spec.ref,
            issue=spec.issue,
            merge_request=spec.merge_request,
            fallback_ref_on_missing=spec.fallback_ref_on_missing,
            sandbox_env_id=spec.sandbox_env_id,
            acting_user_id=spec.acting_user_id,
            mcp_overrides=spec.mcp_overrides,
            references=spec.references,
        ) as ctx,
        open_checkpointer() as checkpointer,
    ):
        if spec.fallback_ref_on_missing and spec.ref and ctx.repo.ref != spec.ref:
            await _repin_fallback_ref(spec.thread_id, ctx.repo.ref)
        if hooks.on_context_ready is not None:
            await hooks.on_context_ready(ctx.repo.ref)
        agent_kwargs = get_daiv_agent_kwargs(
            model_config=ctx.config.models.agent,
            agent_model=spec.agent_model,
            agent_thinking_level=spec.agent_thinking_level,
            **({"use_max": True} if spec.use_max else {}),
        )
        model = agent_kwargs["model_names"][0]
        await _persist_resolved_agent(spec, model=model, thinking_level=agent_kwargs["thinking_level"] or "")
        agent = await create_daiv_agent(ctx=ctx, checkpointer=checkpointer, **agent_kwargs)
        config = build_langsmith_config(
            ctx,
            trigger=spec.trigger,
            model=model,
            thinking_level=agent_kwargs["thinking_level"],
            agent_name=agent.get_name(),
            extra_metadata=spec.extra_metadata,
            configurable={"thread_id": spec.thread_id},
        )
        with track_usage_metadata() as usage:
            yield AgentRun(ctx=ctx, agent=agent, config=config, usage=usage)


async def _repin_fallback_ref(thread_id: str, new_ref: str) -> None:
    """Point the session at the branch the clone fell back to, so the next turn doesn't ask for a branch that is
    gone. Best-effort: the fallback clone already succeeded, so a failed write must not abort the run."""
    from sessions.services import areset_session_ref  # sessions.services imports jobs.tasks, which imports us

    try:
        await areset_session_ref(thread_id=thread_id, new_ref=new_ref)
    except Exception:
        logger.exception("executor: failed to reset session ref for thread_id=%s", thread_id)


async def _after_run(spec: RunSpec, run: AgentRun, *, response_text: str) -> RunOutcome:
    from automation.agent.results import build_agent_result
    from automation.agent.usage_tracking import build_usage_summary
    from sessions.services import apersist_session_ref  # sessions.services imports jobs.tasks, which imports us

    snapshot = await _read_snapshot(run, spec.thread_id)
    values = snapshot.values if snapshot is not None else {}
    merge_request = values.get("merge_request")
    if spec.persist_ref:
        try:
            await apersist_session_ref(
                thread_id=spec.thread_id, current_ref=run.ctx.repo.ref, merge_request=merge_request
            )
        except Exception:
            logger.exception("executor: failed to persist session ref for thread_id=%s", spec.thread_id)
    if spec.arm_watch:
        try:
            await PipelineWatch(spec.repo_id).aarm_after_run(
                run_id=spec.run_id,
                merge_request=merge_request,
                published=bool(values.get("published")),
                user_id=spec.acting_user_id,
            )
        except Exception:
            logger.exception("executor: failed to arm pipeline watch for thread_id=%s", spec.thread_id)

    agent_result = await build_agent_result(
        run.agent, run.config, response=response_text, usage=build_usage_summary(run.usage).to_dict(), snapshot=snapshot
    )
    return RunOutcome(agent_result=agent_result, response_text=response_text, snapshot=snapshot)


async def _read_snapshot(run: AgentRun, thread_id: str) -> StateSnapshot | None:
    """Read the run's checkpoint, or ``None`` when a transport or serialization error breaks the read: the agent
    already finished, so a Redis blip must not fail the run."""
    try:
        return await run.agent.aget_state(config=run.config)
    except RedisError, OSError, json.JSONDecodeError:
        logger.warning("executor: failed to read agent state for thread_id=%s", thread_id, exc_info=True)
        return None


async def _read_snapshot_after_recovery(run: AgentRun, thread_id: str) -> StateSnapshot | None:
    """The re-read only feeds a failure-note footer, so any read error here is safe to swallow: the agent's own
    error is what must reach ``on_failure``, not this one."""
    try:
        return await _read_snapshot(run, thread_id)
    except Exception:
        logger.warning(
            "executor: failed to read agent state after draft recovery for thread_id=%s", thread_id, exc_info=True
        )
        return None


async def _persist_resolved_agent(spec: RunSpec, *, model: str, thinking_level: str) -> None:
    """Overwrite the requested ``agent_model`` on the Run and its Session with the resolved one, so the
    detail view shows what ran instead of the "Auto" pill. Best-effort: a DB error is logged, never raised.
    """
    if not spec.run_id:
        return
    fields = {"agent_model": model, "agent_thinking_level": thinking_level}
    try:
        await Run.objects.filter(pk=spec.run_id).aupdate(**fields)
        await Session.objects.filter(pk=spec.thread_id).aupdate(**fields)
    except Exception:
        logger.exception("executor: failed to persist resolved agent model for thread_id=%s", spec.thread_id)


async def _notify_failure(
    hooks: RunHooks, exc: Exception, *, draft_published: bool = False, snapshot: StateSnapshot | None = None
) -> None:
    if hooks.on_failure is None:
        return
    try:
        await hooks.on_failure(exc, draft_published=draft_published, snapshot=snapshot)
    except Exception:
        logger.exception("executor: on_failure hook failed while handling %s", type(exc).__name__)
