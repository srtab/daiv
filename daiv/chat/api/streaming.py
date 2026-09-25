from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field, fields, is_dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from django.utils import timezone

from ag_ui.core.events import (
    BaseEvent,
    CustomEvent,
    EventType,
    RunErrorEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from copilotkit import LangGraphAGUIAgent
from sessions.executor.lock import Held, SessionLockLostError
from sessions.executor.run import RunStoppedError, stream_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.locks import SessionLock
from sessions.models import Run, RunStatus, SessionOrigin, usage_field_updates

from automation.agent.events import ASSISTANT_MESSAGE_EVENT, parse_assistant_message
from automation.agent.usage_tracking import build_usage_summary
from codebase.base import Scope
from core import ui_events
from core.constants import CANCELLED_BY_USER_MESSAGE, INTERRUPTED_MESSAGE, RUN_FAILED_MESSAGE

from . import relay
from .event_filter import SubagentEventFilter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from ag_ui.core import RunAgentInput
    from sessions.executor.run import AgentRun

    from codebase.context import RuntimeCtx
    from codebase.references import ExternalRef

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
        prompt=prompt[:2000],
        message_id=message_id,
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
    # ``aupdate`` fires no post_save, so the nav badge poke the Run signal would have
    # sent has to be issued here.
    await ui_events.publisher.aruns_changed()


# GitState fields that survive the ag-ui output-schema filter and reach the
# chat client through STATE_SNAPSHOT events.
STREAMED_STATE_KEYS = ("merge_request", "diff_stats")


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

    async def _handle_single_event(self, event: Any, state: dict[str, Any]) -> AsyncGenerator[Any]:
        """Stamp the source LangGraph event's provenance onto any AGUI event upstream
        emits without a ``raw_event``.

        ``ag_ui_langgraph`` builds every event type with ``raw_event=event`` *except*
        the ``REASONING_*`` family (``handle_reasoning_event`` and the redacted-thinking
        ``ReasoningEncryptedValueEvent``), which it emits bare. ``SubagentEventFilter``
        reads provenance exclusively from ``raw_event.metadata.langgraph_checkpoint_ns``,
        so untagged reasoning reads back as top-level: a subagent's *thinking* bleeds
        into the parent turn even though its text and tool calls are correctly dropped.

        Stamping here rather than inferring the namespace in the filter is deliberate,
        but *not* because the filter lacks the information: upstream yields a RAW event
        carrying the same metadata immediately before dispatching each LangGraph event
        (``agent.py`` ``_handle_stream_events``), so a "namespace of the last event that
        had one" tracker would work today. It would just be betting on undocumented
        yield ordering and on that RAW emission staying unconditional. Carrying
        provenance *on* the event removes the bet.

        Only the two keys the filter consumes are copied — stamping the whole metadata
        dict would serialize ~550 bytes of LangGraph internals onto every reasoning
        delta, and a thinking stream emits one event per delta.

        Known limit: upstream tracks reasoning state per *run*, not per namespace
        (``active_run["reasoning_process"]``), so the closing REASONING_END /
        REASONING_ENCRYPTED_VALUE frames are stamped with whatever chunk is in flight
        when they fire. A subagent whose reasoning is still open when its model stream
        ends can therefore emit a stray unmatched END under the parent's namespace —
        a dangling frame, never readable content.
        """
        metadata = event.get("metadata") if isinstance(event, dict) else None
        if not isinstance(metadata, dict):
            metadata = {}
        provenance = {k: metadata[k] for k in ("langgraph_checkpoint_ns", "emit-messages") if k in metadata}

        async for agui_event in super()._handle_single_event(event, state):
            # The isinstance check is always true at runtime — every member of
            # upstream's ``ProcessedEvents`` union subclasses ``BaseEvent``. Keep it
            # anyway: ``copilotkit.LangGraphAGUIAgent`` *declares* this method as
            # yielding ``str``, so it is the only form that narrows the loop variable
            # for ``ty``. Dropping it reintroduces "unresolved attribute raw_event
            # on type str"; a bare ``agui_event: BaseEvent`` declaration conflicts
            # with the inherited signature instead.
            if provenance and isinstance(agui_event, BaseEvent) and agui_event.raw_event is None:
                agui_event.raw_event = {"metadata": provenance}
            yield agui_event

        for text_event in self._assistant_message_frames(event, provenance):
            yield text_event

    @staticmethod
    def _assistant_message_frames(event: Any, provenance: dict) -> list[BaseEvent]:
        """Translate DAIV's ``ASSISTANT_MESSAGE_EVENT`` into the text frames the chat renders.

        See ``automation.agent.utils.streamed_assistant_message`` for why a message the agent
        produced without a model call never streams on its own.

        This belongs on the event hook rather than in ``SubagentEventFilter``: the frames carry the
        source event's ``langgraph_checkpoint_ns``, so the filter already drops them when the
        message came from inside a subagent. Consolidating the two would lose that for free.

        ``ag_ui_langgraph`` ships an equivalent handler for its own ``manually_emit_message``, and
        reusing it would need no import — only that wire name. It is forked anyway, for two reasons
        that are easy to miss: upstream hard-subscripts ``event["data"]["message_id"]``, so a single
        malformed payload raises mid-stream and kills the run, and adopting the name would make an
        undocumented upstream enum load-bearing, where a rename would silently stop these messages
        rendering — the exact failure this whole change exists to fix. Copilotkit's
        ``copilotkit_``-prefixed variant is separately unusable: its handler builds these three
        frames and then discards them, dropping what ``_dispatch_event`` returns instead of
        yielding it.
        """
        if not isinstance(event, dict) or event.get("event") != "on_custom_event":
            return []
        if event.get("name") != ASSISTANT_MESSAGE_EVENT:
            return []
        if (parsed := parse_assistant_message(event.get("data"))) is None:
            logger.warning("chat: malformed %s payload; dropping frames", ASSISTANT_MESSAGE_EVENT)
            return []
        # Only the keys the filter reads: stamping the whole event would ship the LangGraph
        # internals *and* a second copy of the message body on each of the three frames.
        raw_event = {"metadata": provenance}
        return [
            TextMessageStartEvent(
                type=EventType.TEXT_MESSAGE_START, role="assistant", message_id=parsed.message_id, raw_event=raw_event
            ),
            TextMessageContentEvent(
                type=EventType.TEXT_MESSAGE_CONTENT,
                message_id=parsed.message_id,
                delta=parsed.content,
                raw_event=raw_event,
            ),
            TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=parsed.message_id, raw_event=raw_event),
        ]

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


@dataclass
class _Turn:
    """What a chat turn has recorded so far, filled in as the executor reaches each step."""

    chat_run: Run | None = None
    agent_run: AgentRun | None = None
    response: str = ""
    error: str | None = None

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
            hooks = RunHooks(on_context_ready=partial(self._start_turn, turn))
            async with contextlib.aclosing(
                stream_run(
                    self._run_spec(), partial(self._agui_events, turn), hooks, should_stop=run_relay.cancel_requested
                )
            ) as stream:
                async for event in stream:
                    if event.type in (EventType.TEXT_MESSAGE_CONTENT, EventType.TEXT_MESSAGE_CHUNK):
                        turn.add_text(getattr(event, "delta", None))
                    elif event.type == EventType.RUN_ERROR:
                        # Upstream's message can carry raw exception text: it streams live but never reaches
                        # ``Run.error_message``, which the transcript renders verbatim on reload.
                        turn.error = RUN_FAILED_MESSAGE
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
            await self._end_turn(turn, succeeded=finished and turn.error is None)

    def _run_spec(self) -> RunSpec:
        return RunSpec(
            thread_id=self.thread_id,
            repo_id=self.repo_id,
            scope=Scope.GLOBAL,
            input_messages=(),
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

    async def _start_turn(self, turn: _Turn, ref: str) -> None:
        """Record the turn as a RUNNING ``Run`` on the ref the clone landed on."""
        if ref != self.ref:
            logger.warning(
                "chat: ref %r no longer exists for thread_id=%s; fell back to default branch %r",
                self.ref,
                self.thread_id,
                ref,
            )
        turn.chat_run = await start_chat_run(
            session_id=self.thread_id,
            user_id=self.user_id,
            prompt=self.prompt,
            repo_id=self.repo_id,
            ref=ref,
            message_id=self.message_id,
        )

    async def _agui_events(self, turn: _Turn, run: AgentRun) -> AsyncGenerator[BaseEvent]:
        """The turn's AG-UI stream: a ``ref_fallback`` frame when the clone fell back, then the agent's events through
        the subagent filter. Raises ``_ReportedRunError`` after the last event when one of them was a RUN_ERROR."""
        turn.agent_run = run
        if run.ctx.repo.ref != self.ref:
            yield CustomEvent(
                type=EventType.CUSTOM, name="ref_fallback", value={"requested": self.ref, "using": run.ctx.repo.ref}
            )
        agui = RuntimeContextLangGraphAGUIAgent(
            name="DAIV",
            description="DAIV agent",
            graph=run.agent,
            config={"recursion_limit": 500, **run.config},
            runtime_context=run.ctx,
        )
        reported = False
        async with contextlib.aclosing(SubagentEventFilter().apply(agui.run(self.input_data))) as events:
            async for event in events:
                reported = reported or event.type == EventType.RUN_ERROR
                yield event
        if reported:
            raise _ReportedRunError

    async def _end_turn(self, turn: _Turn, *, succeeded: bool) -> None:
        """Finalize the ``Run`` row, then release the slot; each failure is logged, never raised over how the turn
        ended."""
        if turn.chat_run is not None:
            try:
                await finalize_chat_run(
                    turn.chat_run.pk,
                    success=succeeded,
                    usage=build_usage_summary(turn.agent_run.usage).to_dict() if turn.agent_run else None,
                    response_text=turn.response,
                    error_message=turn.error or "",
                )
            except Exception:
                logger.exception("chat: failed to finalize chat run for thread_id=%s", self.thread_id)
        try:
            await SessionLock.release(self.thread_id, self.run_id)
        except Exception:
            logger.exception("chat: failed to release run slot for thread_id=%s", self.thread_id)
