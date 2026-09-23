import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sessions.executor.lock import hold_session_lock
from sessions.executor.spec import RunHooks, RunOutcome
from sessions.models import Run, Session
from sessions.pipeline_watch.service import PipelineWatch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler
    from codebase.context import RuntimeCtx
    from sessions.executor.spec import RunSpec

logger = logging.getLogger("daiv.sessions")


async def execute_run(spec: RunSpec, hooks: RunHooks | None = None) -> RunOutcome:
    """Run the agent once for ``spec``; the package docstring lists the order of the steps."""
    hooks = hooks or RunHooks()
    async with hold_session_lock(spec.lock, spec.thread_id):
        try:
            outcome = await _invoke(spec)
        except Exception as exc:
            await _notify_failure(hooks, exc)
            raise
        if hooks.on_success is not None:
            await hooks.on_success(outcome)
        return outcome


@dataclass(frozen=True)
class _AgentRun:
    ctx: RuntimeCtx
    agent: CompiledAgent
    config: RunnableConfig


async def _invoke(spec: RunSpec) -> RunOutcome:
    from automation.agent.usage_tracking import track_usage_metadata

    async with _agent_run(spec) as run:
        with track_usage_metadata() as usage_handler:
            result = await run.agent.ainvoke(
                {"messages": list(spec.input_messages)}, config=run.config, context=run.ctx
            )
        return await _after_run(spec, run, result, usage_handler)


@asynccontextmanager
async def _agent_run(spec: RunSpec) -> AsyncIterator[_AgentRun]:
    # Imported here so django.setup(), which reaches this module via jobs.tasks, never loads the agent stack.
    from automation.agent.graph import create_daiv_agent
    from automation.agent.utils import build_langsmith_config, get_daiv_agent_kwargs
    from codebase.context import set_runtime_ctx
    from core.checkpointer import open_checkpointer

    async with (
        set_runtime_ctx(
            repo_id=spec.repo_id,
            scope=spec.scope,
            ref=spec.ref,
            sandbox_env_id=spec.sandbox_env_id,
            acting_user_id=spec.acting_user_id,
            mcp_overrides=spec.mcp_overrides,
            references=spec.references,
        ) as ctx,
        open_checkpointer() as checkpointer,
    ):
        agent_kwargs = get_daiv_agent_kwargs(
            model_config=ctx.config.models.agent,
            agent_model=spec.agent_model,
            agent_thinking_level=spec.agent_thinking_level,
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
        yield _AgentRun(ctx=ctx, agent=agent, config=config)


async def _after_run(
    spec: RunSpec, run: _AgentRun, result: dict[str, Any], usage_handler: CostAwareUsageMetadataCallbackHandler
) -> RunOutcome:
    from automation.agent.results import build_agent_result
    from automation.agent.usage_tracking import build_usage_summary
    from automation.agent.utils import extract_text_content
    from sessions.services import apersist_session_ref  # sessions.services imports jobs.tasks, which imports us

    messages = result.get("messages")
    if not messages:
        raise ValueError(f"Agent returned no messages for repo_id={spec.repo_id}")
    response_text = extract_text_content(messages[-1].content)

    snapshot = await run.agent.aget_state(config=run.config)
    merge_request = snapshot.values.get("merge_request")
    if spec.persist_ref:
        try:
            await apersist_session_ref(
                thread_id=spec.thread_id, current_ref=spec.ref or "", merge_request=merge_request
            )
        except Exception:
            logger.exception("executor: failed to persist session ref for thread_id=%s", spec.thread_id)
    if spec.arm_watch:
        try:
            await PipelineWatch(spec.repo_id).aarm_after_run(
                run_id=spec.run_id,
                merge_request=merge_request,
                published=bool(snapshot.values.get("published")),
                user_id=spec.acting_user_id,
            )
        except Exception:
            logger.exception("executor: failed to arm pipeline watch for thread_id=%s", spec.thread_id)

    agent_result = await build_agent_result(
        run.agent,
        run.config,
        response=response_text,
        usage=build_usage_summary(usage_handler).to_dict(),
        snapshot=snapshot,
    )
    return RunOutcome(agent_result=agent_result, response_text=response_text, snapshot=snapshot)


async def _persist_resolved_agent(spec: RunSpec, *, model: str, thinking_level: str) -> None:
    """Overwrite the Run + Session ``agent_model`` with the resolved model/thinking.

    ``agent_model`` normally holds the *requested* override, where empty means "use the site default".
    Once a run has resolved a concrete model it is overwritten with that spec, so the session detail
    view shows what actually ran rather than an empty pill. A run that fails earlier (e.g. the git
    clone inside ``set_runtime_ctx``) leaves the field empty and the UI falls back to the "Auto" pill.
    Best-effort: a cosmetic denormalization must never abort the run, so a DB error is logged.
    """
    if not spec.run_id or not model:
        return
    fields = {"agent_model": model, "agent_thinking_level": thinking_level}
    try:
        await Run.objects.filter(pk=spec.run_id).aupdate(**fields)
        await Session.objects.filter(pk=spec.thread_id).aupdate(**fields)
    except Exception:
        logger.exception("executor: failed to persist resolved agent model for thread_id=%s", spec.thread_id)


async def _notify_failure(hooks: RunHooks, exc: Exception) -> None:
    if hooks.on_failure is None:
        return
    try:
        await hooks.on_failure(exc, draft_published=False)
    except Exception:
        logger.exception("executor: on_failure hook failed while handling %s", type(exc).__name__)
