import asyncio
import contextlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from django.urls import reverse

from asgiref.sync import sync_to_async
from redis.exceptions import RedisError
from redisvl.exceptions import RedisSearchError

from automation.agent.session_links import SessionLinks
from core.utils import build_absolute_url
from sessions.archive import aarchive_thread, arestore_thread
from sessions.artifacts import RunArtifactStore, bind_active_run
from sessions.delegation import adelegator_for
from sessions.executor.lock import SessionLockLostError, hold_session_lock, still_held
from sessions.executor.recovery import recover_draft
from sessions.executor.spec import RunHooks, RunOutcome
from sessions.models import PublishStatus, Run, publish_field_updates
from sessions.pipeline_watch.service import PipelineWatch
from sessions.services import apersist_session_ref, areset_session_ref

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
    from contextlib import AbstractAsyncContextManager

    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.types import StateSnapshot

    from automation.agent.agent_settings import AgentSettings
    from automation.agent.context import RuntimeCtx
    from automation.agent.events import PublishResultRecorder
    from automation.agent.publishers import PublishOutcome
    from automation.agent.session_links import SessionLinksResolver
    from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.session import SandboxSession
    from sessions.executor.spec import RunSpec
    from sessions.live.relay import RunRelay

logger = logging.getLogger("daiv.sessions")

STREAM_HEARTBEAT_INTERVAL_S = 5.0
STOP_POLL_INTERVAL_S = 10.0
ARCHIVE_TIMEOUT_S = 30.0


class RunStoppedError(Exception):
    """``stream_run``'s ``should_stop`` asked the run to stop."""


@dataclass(frozen=True)
class AgentRun:
    """The built agent and what it runs with. ``usage`` tallies the run's tokens and cost, subagents included.
    ``thread_id`` is the graph's thread: the session's, or a fresh one for a one-shot run. ``workspace`` is where the
    agent works; draft recovery publishes through it, and the executor releases its sandbox session. ``settings`` is
    what the run resolved once, and ``session_links`` what builds the links they allow; draft recovery publishes with
    both. ``publish`` keeps the last publish result the run dispatched."""

    ctx: RuntimeCtx
    agent: CompiledStateGraph[Any, RuntimeCtx]
    config: RunnableConfig
    usage: CostAwareUsageMetadataCallbackHandler
    thread_id: str
    workspace: Workspace
    settings: AgentSettings
    session_links: SessionLinksResolver | None
    publish: PublishResultRecorder
    run_id: str | None = None


async def execute_run(
    spec: RunSpec, hooks: RunHooks | None = None, *, should_stop: Callable[[], Awaitable[bool]] | None = None
) -> RunOutcome:
    """Run the agent once for ``spec``; the package docstring lists the order of the steps."""
    hooks = hooks or RunHooks()
    entered = False
    try:
        async with _hold_slot(spec) as holder_id:
            entered = True
            return await _run_in_slot(spec, hooks, holder_id, should_stop)
    except Exception as exc:
        if not entered:
            await _notify_failure(hooks, exc)
        raise


async def stream_run(
    spec: RunSpec,
    stream: Callable[[AgentRun], AsyncGenerator[Any]],
    hooks: RunHooks | None = None,
    *,
    should_stop: Callable[[], Awaitable[bool]],
) -> AsyncGenerator[Any]:
    """Run the agent once for ``spec`` and yield what ``stream`` makes of the built agent, as it comes.

    Consume it with ``contextlib.aclosing``: closing it early (a reader that went away) closes the stream and the
    run's context and skips ``on_success`` and ``on_failure``, as a cancellation mid-stream does; an error while the
    context closes is logged, never raised over the close or the cancellation. The package docstring lists the
    order of the steps.
    """
    hooks = hooks or RunHooks()
    entered = False
    interrupted: BaseException | None = None
    try:
        async with _hold_slot(spec, background_heartbeat=False) as holder_id:
            entered = True
            recovery = _Recovery()
            try:
                async with _agent_run(spec, hooks) as run:
                    try:
                        async with contextlib.aclosing(
                            _supervised(stream(run), run.thread_id, holder_id, should_stop)
                        ) as events:
                            async for event in events:
                                yield event
                    except (GeneratorExit, asyncio.CancelledError) as exc:
                        interrupted = exc
                        raise
                    except Exception:
                        await _recover(spec, run, recovery)
                        raise
                    outcome = await _after_run(spec, run)
            except Exception as exc:
                if interrupted is None:
                    await _notify_failure(
                        hooks, exc, draft_published=recovery.draft_published, snapshot=recovery.snapshot
                    )
                raise
            if hooks.on_success is not None:
                await hooks.on_success(outcome)
    except Exception as exc:
        if interrupted is not None:
            logger.exception("executor: failed to close the run for thread_id=%s", spec.thread_id)
            raise interrupted from None
        if not entered:
            await _notify_failure(hooks, exc)
        raise


def _hold_slot(spec: RunSpec, *, background_heartbeat: bool = True) -> AbstractAsyncContextManager[str | None]:
    """Hold the session's slot under the spec's policy; a one-shot run has no session, so it holds nothing."""
    if spec.thread_id is None:
        return nullcontext()
    return hold_session_lock(spec.lock, spec.thread_id, background_heartbeat=background_heartbeat)


async def _supervised(
    events: AsyncGenerator[Any], thread_id: str, holder_id: str | None, should_stop: Callable[[], Awaitable[bool]]
) -> AsyncGenerator[Any]:
    """Yield ``events``, checking the slot and then ``should_stop`` at most every ``STREAM_HEARTBEAT_INTERVAL_S``.

    A lost slot wins over a stop request: another holder owns the checkpoint now. Either one raises and closes
    ``events``, abandoning the graph run inside it; an error while closing is logged, never raised over why the
    stream stopped.
    """
    last_check = time.monotonic()
    try:
        async for event in events:
            yield event
            now = time.monotonic()
            if now - last_check < STREAM_HEARTBEAT_INTERVAL_S:
                continue
            last_check = now
            if holder_id is not None and not await still_held(thread_id, holder_id):
                raise SessionLockLostError(f"session lock for thread_id={thread_id} was taken over by another holder")
            if await should_stop():
                raise RunStoppedError(f"run for thread_id={thread_id} was asked to stop")
    finally:
        try:
            await events.aclose()
        except Exception:
            logger.exception("executor: failed to close the stream for thread_id=%s", thread_id)


async def _run_in_slot(
    spec: RunSpec, hooks: RunHooks, holder_id: str | None, should_stop: Callable[[], Awaitable[bool]] | None
) -> RunOutcome:
    recovery = _Recovery()
    async with _live_relay(spec, holder_id) as run_relay:
        try:
            invoke = partial(_invoke, spec, hooks, recovery, run_relay)
            outcome = await (invoke() if should_stop is None else _until_stopped(invoke, spec.thread_id, should_stop))
        except Exception as exc:
            await _notify_failure(hooks, exc, draft_published=recovery.draft_published, snapshot=recovery.snapshot)
            raise
        if hooks.on_success is not None:
            await hooks.on_success(outcome)
    return outcome


async def _until_stopped(
    invoke: Callable[[], Coroutine[Any, Any, RunOutcome]],
    thread_id: str | None,
    should_stop: Callable[[], Awaitable[bool]],
) -> RunOutcome:
    """Run ``invoke`` unless ``should_stop`` asks to stop first, asking again every ``STOP_POLL_INTERVAL_S``.

    A stop request raises ``RunStoppedError``, after cancelling the invocation and waiting for it to close the run's
    context if it had started; an invocation that ends while ``should_stop`` is asked keeps its outcome.
    """
    if await should_stop():
        raise RunStoppedError(f"run for thread_id={thread_id} was asked to stop before it started")
    task = asyncio.ensure_future(invoke())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=STOP_POLL_INTERVAL_S)
            if not task.done() and await should_stop() and not task.done():
                raise RunStoppedError(f"run for thread_id={thread_id} was asked to stop")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@asynccontextmanager
async def _live_relay(spec: RunSpec, holder_id: str | None) -> AsyncGenerator[RunRelay | None]:
    """The relay a session run streams to under its slot's holder id, ended once the run has settled; ``None`` for a
    one-shot run, or with no Redis, which runs unpublished."""
    from core.redis import SOCKET_TIMEOUT_S, redis_connections
    from sessions.live.publisher import end_stream
    from sessions.live.relay import RunRelay

    thread_id = spec.thread_id
    if thread_id is None or holder_id is None or not redis_connections.configured:
        yield None
        return
    try:
        client = redis_connections.build_async_client(socket_timeout=SOCKET_TIMEOUT_S)
    except Exception:
        logger.exception("executor: no run relay for thread_id=%s; the run carries on unpublished", thread_id)
        client = None
    if client is None:
        yield None
        return
    run_relay = RunRelay(thread_id, holder_id, client=client)
    try:
        yield run_relay
    finally:
        await end_stream(run_relay)
        try:
            await client.aclose()
        except Exception:
            logger.exception("executor: failed to close the run relay client for thread_id=%s", thread_id)


@dataclass
class _Recovery:
    """What draft recovery leaves for ``on_failure``: filled inside the run's context, read after it closes."""

    draft_published: bool = False
    draft_recorded: bool = False
    snapshot: StateSnapshot | None = None


async def _invoke(spec: RunSpec, hooks: RunHooks, recovery: _Recovery, run_relay: RunRelay | None) -> RunOutcome:
    from automation.agent.utils import extract_text_content

    async with _agent_run(spec, hooks) as run:
        try:
            result = await _ainvoke(run, spec, run_relay)
        except Exception:
            await _recover(spec, run, recovery)
            raise
        messages = result.get("messages")
        if not messages:
            raise ValueError(f"Agent returned no messages for repo_id={spec.repo_id}")
        return await _after_run(spec, run, response_text=extract_text_content(messages[-1].content))


async def _ainvoke(run: AgentRun, spec: RunSpec, run_relay: RunRelay | None) -> dict[str, Any]:
    """Invoke the agent, streaming its events to ``run_relay`` when the run has one."""
    from sessions.live.publisher import ainvoke_live

    graph_input = {"messages": list(spec.input_messages)}
    if run_relay is None:
        return await run.agent.ainvoke(graph_input, config=run.config, context=run.ctx)
    return await ainvoke_live(
        run.agent,
        graph_input,
        config=run.config,
        context=run.ctx,
        recursion_limit=run.settings.recursion_limit,
        run_relay=run_relay,
    )


async def _recover(spec: RunSpec, run: AgentRun, recovery: _Recovery) -> None:
    """Publish a draft from the checkpoint after the agent or its stream raises, if the spec asks for it, and re-read
    the checkpoint for ``on_failure``. Runs while the clone and the sandbox are still open."""
    if spec.recover_draft:
        await _recover_draft(spec, run, recovery)
    await _record_publish_result(run, recovery)


async def _recover_draft(spec: RunSpec, run: AgentRun, recovery: _Recovery) -> None:
    outcome = await recover_draft(
        run.ctx,
        run.agent,
        run.config,
        thread_id=run.thread_id,
        workspace=run.workspace,
        settings=run.settings,
        session_links=run.session_links,
    )
    recovery.draft_published = outcome is not None
    if outcome is not None and outcome.created_merge_request:
        recovery.draft_recorded = await _record_recovered_draft(spec.run_id, outcome)
    recovery.snapshot = await _read_snapshot_after_recovery(run)


async def _record_publish_result(run: AgentRun, recovery: _Recovery) -> None:
    """Keep what this run published before it failed: the last publish result it dispatched goes onto the ``Run``,
    minus the merge request and figures a recovered draft already recorded. Never the checkpoint's, which can be the
    previous run's; a ``failed`` one is the failure's own to record. Best-effort: logged, never raised."""
    publish = run.publish.payload or {}
    if not run.run_id or not publish.get("status") or publish["status"] == PublishStatus.FAILED:
        return
    fields = publish_field_updates(publish)
    if recovery.draft_recorded:
        fields = {k: v for k, v in fields.items() if k.startswith("publish_")}
    try:
        await Run.objects.filter(pk=run.run_id).aupdate(**fields)
    except Exception:
        logger.exception("executor: failed to record the publish result on run_id=%s", run.run_id)


async def _record_recovered_draft(run_id: str | None, outcome: PublishOutcome) -> bool:
    """Record the draft merge request that recovery opened on the failed ``Run``. Best-effort: a failed write is
    logged, never raised over the failure being recovered."""
    merge_request = outcome.merge_request
    if not run_id or merge_request is None:
        return False
    fields: dict[str, Any] = {
        "merge_request_iid": merge_request.merge_request_id,
        "merge_request_web_url": merge_request.web_url or "",
        "code_changes": True,
    }
    if (stats := outcome.diff_stats) is not None:
        fields |= {
            "files_changed": stats.files_changed,
            "lines_added": stats.lines_added,
            "lines_removed": stats.lines_removed,
        }
    try:
        await Run.objects.filter(pk=run_id).aupdate(**fields)
    except Exception:
        logger.exception("executor: failed to record the recovered draft on run_id=%s", run_id)
        return False
    return True


@asynccontextmanager
async def _agent_run(spec: RunSpec, hooks: RunHooks) -> AsyncGenerator[AgentRun]:
    # Deferred so importing this module never loads the agent stack.
    from langgraph.checkpoint.memory import InMemorySaver
    from mcp_connectors.services import build_runtime_servers
    from sandbox_envs.services import build_sandbox_spec

    from automation.agent.agent_settings import resolve_agent_settings
    from automation.agent.context import set_runtime_ctx
    from automation.agent.events import record_publish_result
    from automation.agent.graph import create_daiv_agent
    from automation.agent.middlewares.slash_commands import resets_thread, skips_sandbox
    from automation.agent.usage_tracking import track_usage_metadata
    from automation.agent.utils import build_langsmith_config
    from automation.agent.workspace.session import acquire_sandbox
    from core.checkpointer import open_checkpointer
    from core.site_settings import site_settings

    sandbox_spec = await build_sandbox_spec(spec.sandbox_env_id)
    logger.info(
        "executor: sandbox env=%s enabled=%s for thread_id=%s",
        spec.sandbox_env_id or "<global default>",
        sandbox_spec.enabled,
        spec.thread_id,
    )
    if spec.thread_id is None:
        thread_id, checkpoints = str(uuid.uuid4()), nullcontext(InMemorySaver())
    else:
        thread_id, checkpoints = spec.thread_id, open_checkpointer()
    async with (
        set_runtime_ctx(
            repo_id=spec.repo_id,
            scope=spec.scope,
            ref=spec.ref,
            issue=spec.issue,
            merge_request=spec.merge_request,
            fallback_ref_on_missing=spec.fallback_ref_on_missing,
            sandbox_spec=sandbox_spec,
            acting_user_id=spec.acting_user_id,
            references=spec.references,
            **spec.context_options,
        ) as ctx,
        checkpoints as checkpointer,
    ):
        workspace = _build_workspace(ctx)
        logger.info("executor: %s for thread_id=%s", type(workspace).__name__, thread_id)
        handed_over = False
        run_id = spec.run_id
        try:
            if spec.fallback_ref_on_missing and spec.ref and ctx.repo.ref != spec.ref:
                await _repin_fallback_ref(thread_id, ctx.repo.ref)
            if hooks.on_context_ready is not None:
                started_run_id = await hooks.on_context_ready(ctx.repo.ref)
                run_id = run_id or started_run_id
            settings = resolve_agent_settings(site=site_settings.snapshot(), repo=ctx.config, run=spec.overrides())
            model = settings.agent.names[0]
            await _persist_resolved_agent(
                spec, run_id=run_id, model=model, thinking_level=settings.agent.thinking_level or ""
            )
            session_links = _session_links(spec, settings)
            if workspace.session is not None and not skips_sandbox(spec.input_messages, ctx, settings):
                await acquire_sandbox(workspace.session, ctx, await _checkpointed_values(checkpointer, thread_id))
            delegator = (
                await adelegator_for(spec.thread_id)
                if spec.thread_id is not None and settings.features.orchestration
                else None
            )
            agent = await create_daiv_agent(
                settings=settings,
                ctx=ctx,
                checkpointer=checkpointer,
                ask_user_enabled=spec.ask_user_enabled and spec.thread_id is not None,
                workspace=workspace,
                artifact_store=RunArtifactStore(),
                mcp_servers=await sync_to_async(build_runtime_servers)(spec.acting_user_id, spec.mcp_overrides),
                session_links=session_links,
                delegator=delegator,
                **spec.agent_options,
            )
            config = build_langsmith_config(
                ctx,
                trigger=spec.trigger,
                model=model,
                thinking_level=settings.agent.thinking_level,
                agent_name=agent.get_name(),
                extra_metadata=spec.extra_metadata,
                configurable={"thread_id": thread_id},
            )
            archiving = spec.thread_id is not None
            reset = resets_thread(spec.input_messages, ctx, settings)
            if archiving and not reset:
                await arestore_thread(agent, checkpointer, config)
            with track_usage_metadata() as usage, record_publish_result() as publish, bind_active_run(run_id):
                run = AgentRun(
                    ctx=ctx,
                    agent=agent,
                    config=config,
                    usage=usage,
                    thread_id=thread_id,
                    workspace=workspace,
                    settings=settings,
                    session_links=session_links,
                    publish=publish,
                    run_id=run_id,
                )
                try:
                    yield run
                except SessionLockLostError:
                    archiving = False
                    handed_over = await _handed_over(run)
                    raise
                finally:
                    if archiving:
                        await _archive_thread(checkpointer, thread_id, reset=reset)
        finally:
            try:
                if workspace.session is not None and not handed_over:
                    await _release_sandbox(workspace.session, resumable=spec.thread_id is not None, thread_id=thread_id)
            finally:
                await _record_measurements(run_id, ctx, workspace)


def _build_workspace(ctx: RuntimeCtx) -> Workspace:
    """A sandbox workspace when ``set_runtime_ctx`` opened a sandbox client (its session is acquired later, before the
    agent is built), else a disk workspace over the worker's clone."""
    from automation.agent.workspace.disk import DiskWorkspace
    from automation.agent.workspace.sandbox import SandboxWorkspace
    from automation.agent.workspace.session import SandboxSession

    sandbox_enabled = ctx.sandbox is not None and bool(ctx.sandbox.enabled)
    if sandbox_enabled != (ctx.sandbox_client is not None):
        raise ValueError("The run's sandbox spec and its sandbox client disagree: both or neither must be set")
    if ctx.sandbox is None or ctx.sandbox_client is None:
        return DiskWorkspace(ctx)
    return SandboxWorkspace(SandboxSession(ctx.sandbox_client, ctx.sandbox, credential_source=ctx.credential_source))


def _session_links(spec: RunSpec, settings: AgentSettings) -> SessionLinksResolver | None:
    """What builds the session's links when the run publishes; ``None`` for a one-shot run, which has no session, or
    when the settings turn them off."""
    if spec.thread_id is None or not settings.features.session_link:
        return None
    return partial(_abuild_session_links, spec.thread_id)


async def _abuild_session_links(thread_id: str) -> SessionLinks | None:
    """The session's absolute links; ``None`` when they can't be built: a link is cosmetic, so it never fails the
    publish."""
    kwargs = {"thread_id": thread_id}
    try:
        return await sync_to_async(
            lambda: SessionLinks(
                detail=build_absolute_url(reverse("session_detail", kwargs=kwargs)),
                merge_request=build_absolute_url(reverse("session_merge_request", kwargs=kwargs)),
            )
        )()
    except Exception:
        logger.warning(
            "executor: could not build the session links for thread_id=%s; publishing without them",
            thread_id,
            exc_info=True,
        )
        return None


async def _checkpointed_values(checkpointer: BaseCheckpointSaver, thread_id: str) -> dict[str, Any]:
    """The thread's last checkpoint values; empty for a thread with none (its first turn, or a one-shot run)."""
    checkpoint = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint is None:
        return {}
    return (checkpoint.checkpoint or {}).get("channel_values", {})


async def _handed_over(run: AgentRun) -> bool:
    """Whether the holder that took this run's slot over holds its container: the checkpoint still names it, as that
    holder's reuse leaves it. Stopping it would stop that turn; one the holder replaced is this run's to stop."""
    if run.workspace.session is None or (session_id := run.workspace.session.session_id) is None:
        return False
    try:
        snapshot = await run.agent.aget_state(config=run.config)
    except Exception:
        logger.exception(
            "executor: lost the slot for thread_id=%s and could not read its checkpoint; leaving sandbox session %s "
            "running, as the new holder may be using it",
            run.thread_id,
            session_id,
        )
        return True
    if snapshot.values.get("session_id") != session_id:
        return False
    logger.info(
        "executor: lost the slot for thread_id=%s; leaving sandbox session %s to its new holder",
        run.thread_id,
        session_id,
    )
    return True


async def _archive_thread(checkpointer: BaseCheckpointSaver, thread_id: str, *, reset: bool) -> None:
    """A failure, or a write slower than ``ARCHIVE_TIMEOUT_S``, is logged: archiving never fails a run that already
    ended, nor holds back its release."""
    try:
        async with asyncio.timeout(ARCHIVE_TIMEOUT_S):
            await aarchive_thread(checkpointer, thread_id, reset=reset)
    except Exception:
        logger.exception("executor: failed to archive thread_id=%s", thread_id)


async def _release_sandbox(session: SandboxSession, *, resumable: bool, thread_id: str) -> None:
    """``release`` logs its own close errors; anything else it raises is logged here, so it never fails a finished run
    or replaces a failed run's error."""
    try:
        await session.release(resumable=resumable)
    except Exception:
        logger.exception("executor: failed to release the sandbox session for thread_id=%s", thread_id)


async def _repin_fallback_ref(thread_id: str, new_ref: str) -> None:
    """Point the session at the branch the clone fell back to, so the next turn doesn't ask for a branch that is
    gone. Best-effort: the fallback clone already succeeded, so a failed write must not abort the run."""
    try:
        await areset_session_ref(thread_id=thread_id, new_ref=new_ref)
    except Exception:
        logger.exception("executor: failed to reset session ref for thread_id=%s", thread_id)


async def _after_run(spec: RunSpec, run: AgentRun, *, response_text: str | None = None) -> RunOutcome:
    """``response_text`` is ``None`` for a stream, which has no invoke result: the checkpoint's last message
    stands in."""
    from automation.agent.questions import pending_question
    from automation.agent.usage_tracking import build_usage_summary
    from automation.agent.utils import extract_text_content
    from sessions.executor.result import build_agent_result

    snapshot = await _read_snapshot(run)
    values = snapshot.values if snapshot is not None else {}
    messages = values.get("messages") or []
    if response_text is None:
        response_text = extract_text_content(messages[-1].content) if messages else ""
    merge_request = values.get("merge_request")
    published = bool(values.get("published"))
    # An unread checkpoint must not pass for one that published nothing: that would pin a blank ref to the clone's.
    if spec.persist_ref and snapshot is not None:
        try:
            await apersist_session_ref(
                thread_id=run.thread_id,
                current_ref="" if run.ctx.repo.head_detached else run.ctx.repo.ref,
                merge_request=merge_request,
                published=published,
            )
        except Exception:
            logger.exception("executor: failed to persist session ref for thread_id=%s", run.thread_id)
    if spec.arm_watch:
        try:
            await PipelineWatch(spec.repo_id).aarm_after_run(
                run_id=spec.run_id,
                merge_request=merge_request,
                published=published,
                user_id=spec.acting_user_id,
                sandbox_environment_id=spec.sandbox_env_id,
            )
        except Exception:
            logger.exception("executor: failed to arm pipeline watch for thread_id=%s", run.thread_id)

    agent_result = build_agent_result(
        response=response_text,
        usage=build_usage_summary(run.usage).to_dict(),
        snapshot=snapshot,
        question=pending_question(messages),
    )
    return RunOutcome(agent_result=agent_result, response_text=response_text, snapshot=snapshot)


async def _read_snapshot(run: AgentRun) -> StateSnapshot | None:
    """Read the finished run's checkpoint, or ``None`` when a transport or serialization error breaks the read: the
    agent already finished, so a Redis blip must not fail the run. The checkpointer's index search re-raises a
    Redis error as ``RedisSearchError``."""
    try:
        return await run.agent.aget_state(config=run.config)
    except RedisError, RedisSearchError, OSError, json.JSONDecodeError:
        logger.exception(
            "executor: failed to read the finished run's checkpoint for thread_id=%s; its merge request is lost to "
            "the result, the ref sync and the CI watch",
            run.thread_id,
        )
        return None


async def _read_snapshot_after_recovery(run: AgentRun) -> StateSnapshot | None:
    """The re-read only feeds a failure-note footer, so any read error here is safe to swallow: the agent's own
    error is what must reach ``on_failure``, not this one."""
    try:
        return await run.agent.aget_state(config=run.config)
    except Exception:
        logger.warning(
            "executor: failed to read agent state after draft recovery for thread_id=%s", run.thread_id, exc_info=True
        )
        return None


async def _persist_resolved_agent(spec: RunSpec, *, run_id: str | None, model: str, thinking_level: str) -> None:
    """Overwrite the requested ``agent_model`` on the Run with the resolved one, so the detail view shows what ran
    instead of the "Auto" pill. Never the Session: chat and CI fix runs read ``Session.agent_model`` as the thread's
    pinned override, so writing the resolved model there would pin a model nobody chose.
    Best-effort: a DB error is logged, never raised.
    """
    if not run_id:
        return
    try:
        await Run.objects.filter(pk=run_id).aupdate(agent_model=model, agent_thinking_level=thinking_level)
    except Exception:
        logger.exception("executor: failed to persist resolved agent model for thread_id=%s", spec.thread_id)


async def _record_measurements(run_id: str | None, ctx: RuntimeCtx, workspace: Workspace) -> None:
    """Record the run's clone time and sandbox acquisition on its ``Run``; empty for a disk run, ``NOT_ACQUIRED`` for a
    sandbox run that held no container. Temporary, read by operators only. Best-effort: a failed write is logged."""
    if not run_id:
        return
    acquisition = "" if workspace.session is None else workspace.session.acquisition
    try:
        updated = await Run.objects.filter(pk=run_id).aupdate(
            clone_seconds=ctx.repo.clone_seconds, sandbox_acquisition=acquisition
        )
        if not updated:
            logger.error("executor: no Run row %s to record run measurements on", run_id)
    except Exception:
        logger.exception("executor: failed to record run measurements for run_id=%s", run_id)


async def _notify_failure(
    hooks: RunHooks, exc: Exception, *, draft_published: bool = False, snapshot: StateSnapshot | None = None
) -> None:
    if hooks.on_failure is None:
        return
    try:
        await hooks.on_failure(exc, draft_published=draft_published, snapshot=snapshot)
    except Exception:
        logger.exception("executor: on_failure hook failed while handling %s", type(exc).__name__)
