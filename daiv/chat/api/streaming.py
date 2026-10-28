from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from ag_ui.core.events import BaseEvent, CustomEvent, EventType, RunErrorEvent
from langchain_core.messages import HumanMessage
from sessions.executor.lock import Held, SessionLock, SessionLockLostError
from sessions.executor.run import RunStoppedError, stream_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.live import relay
from sessions.live.agui import RuntimeContextLangGraphAGUIAgent
from sessions.live.event_filter import SubagentEventFilter
from sessions.live.publisher import close_graph_stream, sanitize_run_error
from sessions.models import (
    CHAT_PROMPT_MAX_LENGTH,
    Run,
    RunStatus,
    SessionOrigin,
    agent_result_field_updates,
    usage_field_updates,
)

from automation.agent.usage_tracking import build_usage_summary
from codebase.base import Scope
from core.constants import CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE, RUN_FAILED_MESSAGE

from .threads import _last_user_message

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from ag_ui.core import RunAgentInput
    from sessions.executor.result import AgentResult
    from sessions.executor.run import AgentRun
    from sessions.executor.spec import RunOutcome

    from automation.agent.references import ExternalRef
    from automation.agent.usage_tracking import CostAwareUsageMetadataCallbackHandler

logger = logging.getLogger("daiv.chat")


async def start_chat_run(*, session_id: str, user_id, prompt: str, repo_id: str, ref: str, message_id: str = "") -> Run:
    """Record the chat turn as a RUNNING Run. Chat runs execute inline: no
    task_result, no QUEUED/READY phase.
    """
    return await Run.objects.acreate(
        session_id=session_id,
        trigger_type=SessionOrigin.CHAT,
        status=RunStatus.RUNNING,
        user_id=user_id,
        prompt=prompt[:CHAT_PROMPT_MAX_LENGTH],
        message_id=message_id,
        repo_id=repo_id,
        ref=ref,
        started_at=timezone.now(),
    )


async def finalize_chat_run(
    run: Run,
    *,
    success: bool,
    usage: dict | None,
    response_text: str,
    error_message: str = "",
    question: dict | None = None,
    agent_result: AgentResult | None = None,
) -> None:
    """Terminal transition for a chat Run. Reuses ``usage_field_updates`` and ``agent_result_field_updates`` so
    the denormalization stays identical to the task-backed path (``Run.sync_from_task_result``). On failure,
    ``error_message`` is persisted so the run timeline shows a reason instead of a blank FAILED pill.
    """
    if not success:
        status = RunStatus.FAILED
    elif question is not None:
        status = RunStatus.WAITING_INPUT
    else:
        status = RunStatus.SUCCESSFUL
    update: dict[str, Any] = {"finished_at": timezone.now()}
    if status == RunStatus.WAITING_INPUT:
        update["question"] = question
    if response_text:
        update["result_summary"] = response_text[:2000]
    if not success and error_message:
        update["error_message"] = error_message[:2000]
    if agent_result:
        update.update(agent_result_field_updates(agent_result))
    usage_fields = usage_field_updates(usage, run_ref=run.pk) if usage else {}
    if await Run.objects.afinish(run, status, **update, **usage_fields):
        return
    logger.warning(
        "finalize_chat_run: run=%s finished before its stream did; its %s outcome is dropped", run.pk, status
    )
    if usage_fields:
        # The tokens were spent whichever writer finished the run.
        await Run.objects.filter(pk=run.pk).aupdate(**usage_fields)


@dataclass
class _Turn:
    """What a chat turn has recorded so far, filled in as the executor reaches each step."""

    chat_run: Run | None = None
    usage: CostAwareUsageMetadataCallbackHandler | None = None
    response: str = ""
    error: str | None = None
    question: dict | None = None
    agent_result: AgentResult | None = None

    def add_text(self, delta: str | None) -> None:
        """Buffer assistant text for ``result_summary``, capped at the 2000 chars ``finalize_chat_run`` keeps."""
        if delta and len(self.response) < 2000:
            self.response = (self.response + delta)[:2000]


class _ReportedRunError(Exception):
    """The AG-UI stream already carried this run's RUN_ERROR; raised once the stream ends so the executor fails the
    run without the chat sending a second one."""


@dataclass(frozen=True, kw_only=True)
class ChatRunStreamer:
    """AG-UI event generator for one chat turn, run through the executor's ``stream_run``; ``runner.run_to_relay``
    publishes it.

    Chat owns: the ``resolved_env`` and ``ref_fallback`` events, the ``Run`` row (started once the
    clone is ready, finalized with token/cost usage), RUN_ERROR events with sanitized messages, and releasing the run
    slot the view claimed — after the ``Run`` row is final, so a new turn never finds this one still running.
    """

    repo_id: str
    ref: str
    thread_id: str
    run_id: str
    input_data: RunAgentInput
    user_id: int | None = None
    prompt: str = ""
    message_id: str = ""
    sandbox_environment_id: str | None = None
    agent_model: str | None = None
    agent_thinking_level: str | None = None
    mcp_overrides: dict = field(default_factory=dict)
    external_refs: tuple[ExternalRef, ...] = ()
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

    async def events(self) -> AsyncGenerator[BaseEvent]:
        turn = _Turn()
        run_relay = relay.RunRelay(self.thread_id, self.run_id)
        finished = False
        try:
            # Before the clone, so the locked composer pill swaps "Auto" for the env's name at once and still shows
            # what would have run if setup fails.
            if self.auto_resolved_env is not None:
                yield CustomEvent(type=EventType.CUSTOM, name="resolved_env", value=self.auto_resolved_env)
            hooks = RunHooks(
                on_context_ready=partial(self._start_turn, turn), on_success=partial(self._record_outcome, turn)
            )
            async with contextlib.aclosing(
                stream_run(
                    self._run_spec(), partial(self._agui_events, turn), hooks, should_stop=run_relay.cancel_requested
                )
            ) as stream:
                async for event in stream:
                    yield event
            finished = True
        except _ReportedRunError:
            pass
        except SessionLockLostError:
            logger.warning(
                "chat: lost run slot mid-stream (stale takeover) for thread_id=%s run_id=%s; stopping",
                self.thread_id,
                self.run_id,
            )
            turn.error = turn.error or INTERRUPTED_MESSAGE
            yield RunErrorEvent(type=EventType.RUN_ERROR, message=INTERRUPTED_MESSAGE, code="run_interrupted")
        except RunStoppedError:
            turn.error = CANCELLED_BY_USER_MESSAGE
            yield RunErrorEvent(type=EventType.RUN_ERROR, message=CANCELLED_BY_USER_MESSAGE, code="run_cancelled")
        except asyncio.CancelledError, GeneratorExit:
            # A local Stop, a shutdown, or a reader that goes away: only a user Stop sets the cancel
            # flag, so it decides the recorded reason.
            if turn.error is None:
                turn.error = INTERRUPTED_MESSAGE
                with contextlib.suppress(Exception):
                    if await run_relay.cancel_requested():
                        turn.error = CANCELLED_BY_USER_MESSAGE
            raise
        except Exception:
            # The exception stays in the server log: its text can carry internal names and paths.
            logger.exception("Chat run failed for thread_id=%s run_id=%s", self.thread_id, self.run_id)
            turn.error = RUN_FAILED_MESSAGE
            yield RunErrorEvent(type=EventType.RUN_ERROR, message=RUN_FAILED_MESSAGE, code="run_failed")
        finally:
            await self._end_turn(turn, succeeded=finished)

    def _run_spec(self) -> RunSpec:
        return RunSpec(
            thread_id=self.thread_id,
            repo_id=self.repo_id,
            scope=Scope.GLOBAL,
            input_messages=(HumanMessage(content=self.prompt),),
            trigger="chat",
            lock=Held(holder_id=self.run_id),
            ref=self.ref,
            fallback_ref_on_missing=True,
            agent_model=self.agent_model,
            agent_thinking_level=self.agent_thinking_level,
            sandbox_env_id=self.sandbox_environment_id,
            acting_user_id=self.user_id,
            mcp_overrides=self.mcp_overrides,
            references=self.external_refs,
            persist_ref=True,
            arm_watch=True,
            extra_metadata={"override_source": "explicit" if self.agent_model else None},
        )

    async def _start_turn(self, turn: _Turn, ref: str) -> str:
        """Record the turn as a RUNNING ``Run`` on the ref the clone landed on; return its id for the executor to bind
        as the run's ``Run``."""
        run = await start_chat_run(
            session_id=self.thread_id,
            user_id=self.user_id,
            prompt=self.prompt,
            repo_id=self.repo_id,
            ref=ref,
            message_id=self.message_id,
        )
        turn.chat_run = run
        return str(run.pk)

    async def _record_outcome(self, turn: _Turn, outcome: RunOutcome) -> None:
        """A turn that ended on a question keeps the rendered question as its summary, since the question
        itself was never streamed as text."""
        turn.agent_result = outcome.agent_result
        if (question := outcome.agent_result["question"]) is not None:
            turn.question = question
            turn.response = outcome.response_text

    async def _agui_events(self, turn: _Turn, run: AgentRun) -> AsyncGenerator[BaseEvent]:
        """The turn's AG-UI stream: a ``ref_fallback`` frame when the clone fell back, then the agent's events through
        the subagent filter, recorded on ``turn``. Raises ``_ReportedRunError`` after the last event when one of them
        was a RUN_ERROR."""
        turn.usage = run.usage
        if run.ctx.repo.ref != self.ref:
            yield CustomEvent(
                type=EventType.CUSTOM, name="ref_fallback", value={"requested": self.ref, "using": run.ctx.repo.ref}
            )
        agui = RuntimeContextLangGraphAGUIAgent(
            name="DAIV",
            description="DAIV agent",
            graph=run.agent,
            # ag-ui's run config otherwise carries langchain-core's default (25) over the limit graph.py binds.
            config={"recursion_limit": run.settings.recursion_limit, **run.config},
            runtime_context=run.ctx,
        )
        # The client echoes every user turn it shows, pre-/clear and failed ones included; ag-ui would append each one
        # the checkpoint lacks to the graph input.
        turn_message = _last_user_message(self.input_data)
        agui_input = self.input_data.model_copy(update={"messages": [turn_message] if turn_message else []})
        try:
            async with contextlib.aclosing(SubagentEventFilter().apply(agui.run(agui_input))) as events:
                async for event in events:
                    if event.type in (EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_CHUNK):
                        turn.add_text(getattr(event, "delta", None))
                    elif event.type == EventType.RUN_ERROR:
                        turn.error = RUN_FAILED_MESSAGE
                    yield sanitize_run_error(event)
        finally:
            if agui.graph_stream is not None:
                await close_graph_stream(agui.graph_stream, thread_id=self.thread_id, run_id=self.run_id)
        if turn.error is not None:
            raise _ReportedRunError

    async def _end_turn(self, turn: _Turn, *, succeeded: bool) -> None:
        """Finalize the ``Run`` row, then release the slot; each failure is logged, never raised over how the turn
        ended."""
        if turn.chat_run is not None:
            try:
                await finalize_chat_run(
                    turn.chat_run,
                    success=succeeded,
                    usage=build_usage_summary(turn.usage).to_dict() if turn.usage is not None else None,
                    response_text=turn.response,
                    error_message=turn.error or "",
                    question=turn.question,
                    agent_result=turn.agent_result,
                )
            except Exception:
                logger.exception("chat: failed to finalize chat run for thread_id=%s", self.thread_id)
        try:
            await SessionLock.release(self.thread_id, self.run_id)
        except Exception:
            logger.exception("chat: failed to release run slot for thread_id=%s", self.thread_id)
