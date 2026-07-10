from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, fields, is_dataclass
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from ag_ui.core.events import CustomEvent, EventType, RunErrorEvent
from copilotkit import LangGraphAGUIAgent
from langgraph.store.memory import InMemoryStore
from sessions.locks import SessionLock
from sessions.models import Run, RunStatus, SessionOrigin, usage_field_updates

from automation.agent.graph import create_daiv_agent
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from automation.agent.utils import build_langsmith_config, get_daiv_agent_kwargs
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.checkpointer import open_checkpointer

from . import relay
from .event_filter import SubagentEventFilter
from .threads import ChatSessionService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ag_ui.core import RunAgentInput
    from ag_ui.core.events import BaseEvent

    from codebase.base import MergeRequest
    from codebase.context import RuntimeCtx

logger = logging.getLogger("daiv.chat")


async def start_chat_run(*, session_id: str, user_id, prompt: str, repo_id: str, ref: str) -> Run:
    """Record the chat turn as a RUNNING Run. Chat runs execute inline: no
    task_result, no QUEUED/READY phase.
    """
    return await Run.objects.acreate(
        session_id=session_id,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.RUNNING,
        user_id=user_id,
        prompt=prompt[:2000],
        repo_id=repo_id,
        ref=ref,
        started_at=timezone.now(),
    )


async def finalize_chat_run(
    run_pk, *, success: bool, usage: dict | None, response_text: str, error_message: str = ""
) -> None:
    """Terminal transition for a chat Run. Reuses ``usage_field_updates`` so the
    token/cost denormalization stays identical to the task-backed path
    (``Run.sync_from_task_result``). On failure, ``error_message`` is persisted so the
    run timeline shows a reason instead of a blank FAILED pill.
    """
    update = {"status": RunStatus.SUCCESSFUL if success else RunStatus.FAILED, "finished_at": timezone.now()}
    if response_text:
        update["result_summary"] = response_text[:2000]
    if not success and error_message:
        update["error_message"] = error_message[:2000]
    if usage:
        update.update(usage_field_updates(usage, run_ref=run_pk))
    await Run.objects.filter(pk=run_pk).aupdate(**update)


# GitState fields that survive the ag-ui output-schema filter and reach the
# chat client through STATE_SNAPSHOT events.
STREAMED_STATE_KEYS = ("merge_request",)

# Bump ``last_active_at`` at most this often while the stream is alive.
HEARTBEAT_INTERVAL_S = 5.0

# User-facing terminal messages for stopped runs. The flag-based path is always
# user-initiated; a hard task cancel can also be a process shutdown, so it gets
# the neutral wording.
CANCELLED_BY_USER_MESSAGE = "Stopped by user."
INTERRUPTED_MESSAGE = "Run was interrupted before completing."


class RuntimeContextLangGraphAGUIAgent(LangGraphAGUIAgent):
    """Inject the daiv RuntimeCtx dataclass into upstream's stream kwargs.

    Upstream's ``get_stream_kwargs`` only accepts dict-shaped contexts, but our graph
    declares ``context_schema=RuntimeCtx`` and expects the frozen dataclass itself.
    """

    def __init__(self, *, runtime_context: RuntimeCtx, **kwargs: Any):
        super().__init__(**kwargs)
        self._runtime_context = runtime_context

    def get_stream_kwargs(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        stream_kwargs = super().get_stream_kwargs(*args, **kwargs)
        # Hard-assign (not setdefault): newer LangGraph makes upstream synthesize
        # ``context={"thread_id": ...}`` from ``config['configurable']``, and LangGraph coerces a
        # dict context via ``context_schema(**context)`` -> ``RuntimeCtx(thread_id=...)`` -> TypeError.
        # Passing the dataclass instance skips coercion; ``thread_id`` still reaches the checkpointer
        # through ``config['configurable']``.
        stream_kwargs["context"] = self._runtime_context
        return stream_kwargs

    def get_schema_keys(self, config: Any) -> dict[str, list[str]]:
        # Upstream calls ``graph.config_schema().schema()`` which recurses into
        # ``context_schema=RuntimeCtx``. RuntimeCtx holds a ``git.Repo`` field that
        # pydantic cannot turn into JSON schema, so the call raises
        # PydanticInvalidForJsonSchema. Derive context keys from the dataclass directly.
        ctx_schema = getattr(self.graph, "context_schema", None)
        if not is_dataclass(ctx_schema):
            logger.warning(
                "chat: context_schema %r is not a dataclass; STATE_SNAPSHOT context keys will be empty", ctx_schema
            )
            context_keys: list[str] = []
        else:
            context_keys = [f.name for f in fields(ctx_schema)]
        constant = list(self.constant_schema_keys)
        return {"input": constant, "output": [*constant, *STREAMED_STATE_KEYS], "config": [], "context": context_keys}


@dataclass(frozen=True, kw_only=True)
class ChatRunStreamer:
    """SSE generator: configures the agent, runs it through the subagent filter,
    captures the latest MR from STATE_SNAPSHOTs, records the turn as a ``Run`` with
    token/cost usage, and persists the ref before releasing the per-session run slot.
    """

    repo_id: str
    ref: str
    thread_id: str
    run_id: str
    input_data: RunAgentInput
    user_id: int | None = None
    prompt: str = ""
    sandbox_environment_id: str | None = None
    agent_model: str | None = None
    agent_thinking_level: str | None = None
    # When set, ``{id, name, scope}`` of the env the view auto-resolved for this run.
    # The chat composer's locked pill is still showing "Auto" on the client; the
    # streamer's first emit swaps it to the real name without waiting for a page
    # refresh. ``None`` skips the emit — the view decides when emission is meaningful.
    auto_resolved_env: dict[str, str] | None = None

    def __post_init__(self) -> None:
        # The view passes thread_id/run_id alongside input_data; a future refactor
        # could desync them silently. Pin the invariant here.
        if self.thread_id != self.input_data.thread_id:
            raise ValueError(f"thread_id mismatch: {self.thread_id!r} vs input_data {self.input_data.thread_id!r}")
        if self.run_id != self.input_data.run_id:
            raise ValueError(f"run_id mismatch: {self.run_id!r} vs input_data {self.input_data.run_id!r}")

    async def events(self) -> AsyncIterator[BaseEvent]:
        last_mr: MergeRequest | None = None
        clean_run = False
        # Set when the agent surfaces a failure. ``ag_ui_langgraph`` reports a LangGraph
        # stream error as a RUN_ERROR *event* and then returns normally (it does not raise),
        # so a clean loop exit is not sufficient proof of success — this flag is folded into
        # the finalize decision so an errored turn is recorded FAILED, not SUCCESSFUL.
        run_error_message: str | None = None
        last_heartbeat = time.monotonic()
        # The Run row (a separate object from the AG-UI run_id that holds the lock).
        # Created after the stream context opens; finalized in ``finally``.
        chat_run: Run | None = None
        usage_handler = None
        response_buffer = ""
        try:
            # Surface the auto-resolved env before any agent output so the locked composer
            # pill swaps "Auto" → real env name as early as possible. Kept inside the
            # ``try`` so an encode failure still routes through RUN_ERROR + lock release
            # in ``finally``; the emit precedes ``set_runtime_ctx`` so the user still sees
            # what would have run even if agent setup fails.
            if self.auto_resolved_env is not None:
                yield CustomEvent(type=EventType.CUSTOM, name="resolved_env", value=self.auto_resolved_env)
            async with (
                open_checkpointer() as checkpointer,
                set_runtime_ctx(
                    repo_id=self.repo_id, scope=Scope.GLOBAL, ref=self.ref, sandbox_env_id=self.sandbox_environment_id
                ) as runtime_ctx,
            ):
                # Record the turn as a RUNNING Run once we're committed to executing.
                chat_run = await start_chat_run(
                    session_id=self.thread_id,
                    user_id=self.user_id,
                    prompt=self.prompt,
                    repo_id=self.repo_id,
                    ref=self.ref,
                )
                agent_kwargs = get_daiv_agent_kwargs(
                    model_config=runtime_ctx.config.models.agent,
                    agent_model=self.agent_model,
                    agent_thinking_level=self.agent_thinking_level,
                )
                agent = await create_daiv_agent(
                    ctx=runtime_ctx, checkpointer=checkpointer, store=InMemoryStore(), **agent_kwargs
                )
                langsmith_config = build_langsmith_config(
                    runtime_ctx,
                    trigger="chat",
                    model=agent_kwargs["model_names"][0],
                    thinking_level=agent_kwargs["thinking_level"],
                    agent_name=agent.get_name(),
                    extra_metadata={"override_source": "explicit" if self.agent_model else None},
                )
                langgraph_agent = RuntimeContextLangGraphAGUIAgent(
                    name="DAIV",
                    description="DAIV agent",
                    graph=agent,
                    config={"recursion_limit": 500, **langsmith_config},
                    runtime_context=runtime_ctx,
                )
                # ``track_usage_metadata`` sets a ContextVar whose hook propagates the
                # cost-aware callback to every nested runnable (subagents included) — the
                # same mechanism ``run_job_task`` relies on. The whole generator body runs
                # in one task, so the ContextVar scope holds across ``yield``.
                with track_usage_metadata() as usage_handler:
                    stream = SubagentEventFilter().apply(langgraph_agent.run(self.input_data))
                    try:
                        async for event in stream:
                            if event.type == EventType.STATE_SNAPSHOT:
                                snap = getattr(event, "snapshot", None) or {}
                                if isinstance(snap, dict) and "merge_request" in snap:
                                    last_mr = snap["merge_request"]
                            elif event.type in (EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_CHUNK):
                                # Buffer the assistant text deltas for ``result_summary``. Capped at
                                # 2000 chars — the same bound ``finalize_chat_run`` re-applies.
                                delta = getattr(event, "delta", None)
                                if delta and len(response_buffer) < 2000:
                                    response_buffer = (response_buffer + delta)[:2000]
                            elif event.type == EventType.RUN_ERROR:
                                # Agent failure surfaced as an event (no raise) — capture the reason
                                # so the finally block finalizes FAILED and records it.
                                run_error_message = getattr(event, "message", None) or "Run failed."
                            yield event

                            now = time.monotonic()
                            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                                last_heartbeat = now
                                try:
                                    await SessionLock.heartbeat(self.thread_id, self.run_id)
                                except Exception:
                                    logger.exception("chat: heartbeat failed for thread_id=%s", self.thread_id)
                                if await relay.cancel_requested(self.thread_id, self.run_id):
                                    run_error_message = CANCELLED_BY_USER_MESSAGE
                                    yield RunErrorEvent(
                                        type=EventType.RUN_ERROR,
                                        message=CANCELLED_BY_USER_MESSAGE,
                                        code="run_cancelled",
                                    )
                                    break
                    finally:
                        # No-op when exhausted; on a cancel ``break`` this closes the
                        # in-flight graph invocation (the actual work cancellation).
                        await stream.aclose()
                clean_run = True
        except asyncio.CancelledError:
            # Local hard-cancel (stop endpoint hit this process) or shutdown.
            # Record the reason for the finally block, then propagate — the
            # publisher's finally still emits the stream sentinel.
            run_error_message = run_error_message or INTERRUPTED_MESSAGE
            raise
        except Exception as exc:
            logger.exception("Chat run failed for thread_id=%s run_id=%s", self.thread_id, self.run_id)
            run_error_message = f"{type(exc).__name__}: {exc}"
            yield RunErrorEvent(
                type=EventType.RUN_ERROR, message="Run failed. Check server logs for details.", code="run_failed"
            )
        finally:
            # A run is a success only if the loop finished cleanly AND the agent did not
            # surface a RUN_ERROR event mid-stream (ag_ui reports stream errors as events,
            # not exceptions). Each cleanup step is wrapped independently: a post-stream DB
            # hiccup must not retroactively paint a clean run as RUN_ERROR, and a lock-release
            # failure must not leave the per-session slot permanently claimed. ref is only
            # persisted on success — a failed/partial run could have checked out a branch
            # without committing, and pinning it would silently retarget reloads at half-built state.
            succeeded = clean_run and run_error_message is None
            if succeeded:
                try:
                    await ChatSessionService.persist_ref(self.thread_id, self.ref, last_mr)
                except Exception:
                    logger.exception("chat: failed to persist session ref for thread_id=%s", self.thread_id)
            if chat_run is not None:
                try:
                    await finalize_chat_run(
                        chat_run.pk,
                        success=succeeded,
                        usage=build_usage_summary(usage_handler).to_dict() if usage_handler else None,
                        response_text=response_buffer,
                        error_message=run_error_message or "",
                    )
                except Exception:
                    logger.exception("chat: failed to finalize chat run for thread_id=%s", self.thread_id)
            try:
                await SessionLock.release(self.thread_id, self.run_id)
            except Exception:
                logger.exception("chat: failed to release run slot for thread_id=%s", self.thread_id)
