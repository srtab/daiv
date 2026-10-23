from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ag_ui.core import RunAgentInput
from ag_ui.core.events import EventType, RunErrorEvent, StateSnapshotEvent
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables.config import merge_configs

from core.constants import RUN_FAILED_MESSAGE

from .agui import STREAMED_STATE_KEYS, RuntimeContextLangGraphAGUIAgent
from .event_filter import SubagentEventFilter

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable
    from uuid import UUID

    from ag_ui.core.events import BaseEvent
    from langchain.agents import CompiledAgent
    from langchain_core.runnables import RunnableConfig

    from automation.agent.context import RuntimeCtx

    from .relay import RunRelay

logger = logging.getLogger("daiv.sessions")

_driving_live_run: contextvars.ContextVar[_RootOutcome | None] = contextvars.ContextVar(
    "driving_live_run", default=None
)


class _DropReraisedRunFailure(logging.Filter):
    """Drops ag-ui's logs of the graph's own exception, whatever their message, which ``ainvoke_live`` re-raises for
    the executor to report; ag-ui's other failures are still logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        outcome = _driving_live_run.get()
        if outcome is None or outcome.error is None or not record.exc_info:
            return True
        return record.exc_info[1] is not outcome.error


logging.getLogger("ag_ui_langgraph.agent").addFilter(_DropReraisedRunFailure())


class LiveRunError(Exception):
    """The live stream ended without the graph returning or raising: ag-ui failed around it, or never ran it."""


def sanitize_run_error(event: BaseEvent) -> BaseEvent:
    """``event``, with the generic RUN_ERROR in place of ag-ui's, whose message is the raw exception text: anyone who
    can view the session can read its stream."""
    if event.type == EventType.RUN_ERROR:
        return RunErrorEvent(type=EventType.RUN_ERROR, message=RUN_FAILED_MESSAGE, code="run_failed")
    return event


async def publish_event(run_relay: RunRelay, event: BaseEvent) -> None:
    """Serialize one AG-UI event to the relay, stamped with its emit time.

    The chat client clocks a turn's reasoning segments from ``timestamp`` (epoch ms): a
    rejoining browser replays the whole run at once, so its own clock would measure every
    thought as instantaneous. Only this side knows when the event actually happened.

    RAW events, MESSAGES_SNAPSHOT, every event's ``raw_event`` copy and every STATE_SNAPSHOT key but
    ``STREAMED_STATE_KEYS`` stay off the wire: no viewer reads them, and they carry the model's full input or the whole
    thread. The subagent filter reads them before this point.
    """
    if event.type in (EventType.RAW, EventType.MESSAGES_SNAPSHOT):
        return
    if isinstance(event, StateSnapshotEvent):
        snapshot = event.snapshot or {}
        event = event.model_copy(update={"snapshot": {k: v for k, v in snapshot.items() if k in STREAMED_STATE_KEYS}})
    event.timestamp = event.timestamp or int(time.time() * 1000)
    await run_relay.publish_event(event.model_dump_json(by_alias=True, exclude_none=True, exclude={"raw_event"}))


async def ainvoke_live(
    agent: CompiledAgent,
    graph_input: dict[str, Any],
    *,
    config: RunnableConfig,
    context: RuntimeCtx,
    recursion_limit: int,
    run_relay: RunRelay,
) -> dict[str, Any]:
    """Run ``agent`` as ``agent.ainvoke(graph_input, config=config, context=context)`` would, streaming its AG-UI
    events to ``run_relay`` as it goes.

    The graph's own outcome decides the call: its output is returned and its exception re-raised as is, though one
    that is not an ``Exception`` (the graph's run cancelled under it) raises ``LiveRunError``. Translation and the relay
    are best-effort: when ag-ui fails mid-run the graph still runs to its end, and its RUN_ERROR is published only if
    the graph does not return. The first relay failure is logged and ends the stream, so viewers fall back to polling,
    and the run carries on unpublished. No graph run outlives the call. A RUN_ERROR is published with the generic
    message, since viewers other than the run's owner can read the stream. The caller ends the stream (``end_stream``)
    once the run has settled.
    """
    outcome = _RootOutcome()
    publishing = await _publish(run_relay, run_relay.clear())
    agui = RuntimeContextLangGraphAGUIAgent(
        name="DAIV",
        description="DAIV agent",
        graph=agent,
        config=merge_configs(
            {"recursion_limit": recursion_limit, **config}, {"callbacks": [outcome], "run_id": outcome.root_id}
        ),
        runtime_context=context,
        graph_input=graph_input,
    )
    run_input = RunAgentInput(
        thread_id=config["configurable"]["thread_id"],
        run_id=run_relay.run_id,
        state={},
        messages=[],
        tools=[],
        context=[],
        forwarded_props={},
    )
    run_error = None
    withheld_error: RunErrorEvent | None = None
    token = _driving_live_run.set(outcome)
    try:
        async with contextlib.aclosing(SubagentEventFilter().apply(agui.run(run_input))) as events:
            async for event in events:
                if isinstance(event, RunErrorEvent):
                    run_error = event.message
                    if outcome.error is None:
                        withheld_error = event
                        continue
                publishing = publishing and await _publish(
                    run_relay, publish_event(run_relay, sanitize_run_error(event))
                )
        if outcome.output is None and outcome.error is None and agui.graph_stream is not None:
            async for _ in agui.graph_stream:
                pass
    finally:
        _driving_live_run.reset(token)
        if agui.graph_stream is not None:
            await close_graph_stream(agui.graph_stream, thread_id=run_relay.thread_id, run_id=run_relay.run_id)
        if withheld_error is not None and outcome.output is None and publishing:
            await _publish(run_relay, publish_event(run_relay, sanitize_run_error(withheld_error)))
    if isinstance(outcome.error, Exception):
        raise outcome.error
    if outcome.error is not None:
        raise LiveRunError(f"the graph run ended with {type(outcome.error).__name__}") from outcome.error
    if outcome.output is not None:
        return outcome.output
    raise LiveRunError(run_error or "graph produced no result")


async def close_graph_stream(stream: AsyncGenerator[Any], *, thread_id: str, run_id: str) -> None:
    """Close the graph's stream, cancelling a graph run still going; a failure is logged, never raised over how the
    call ended."""
    try:
        await stream.aclose()
    except Exception:
        logger.exception("live: closing the graph run failed for thread_id=%s run_id=%s", thread_id, run_id)


async def end_stream(run_relay: RunRelay) -> None:
    """Publish the end sentinel, best-effort: a reader whose stream never ends still stops once the slot is released."""
    try:
        await run_relay.publish_end()
    except Exception:
        logger.warning(
            "live: could not end the run stream for thread_id=%s run_id=%s", run_relay.thread_id, run_relay.run_id
        )


async def _publish(run_relay: RunRelay, operation: Awaitable[None]) -> bool:
    """Run one relay write; a failure ends the stream so its viewers go back to polling the run."""
    if await _attempt(run_relay, operation):
        return True
    await end_stream(run_relay)
    return False


async def _attempt(run_relay: RunRelay, operation: Awaitable[None]) -> bool:
    try:
        await operation
    except Exception:
        logger.exception(
            "live: relay failed for thread_id=%s run_id=%s; the run carries on unpublished",
            run_relay.thread_id,
            run_relay.run_id,
        )
        return False
    return True


class _RootOutcome(AsyncCallbackHandler):
    """The root run's output or error: ag-ui reports a failed graph only as a RUN_ERROR with the exception's text.

    The root is the run started with ``root_id`` as its run id; it can have a parent of its own, the caller's
    LangSmith trace or callbacks.
    """

    def __init__(self) -> None:
        self.root_id = uuid4()
        self.output: dict[str, Any] | None = None
        self.error: BaseException | None = None

    async def on_chain_end(self, outputs: dict[str, Any], *, run_id: UUID, **kwargs: Any) -> None:
        if run_id == self.root_id:
            self.output = outputs

    async def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if run_id == self.root_id:
            self.error = error
