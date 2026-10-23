from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import TYPE_CHECKING, Any

from ag_ui.core import RunAgentInput
from ag_ui.core.events import EventType, RunErrorEvent
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables.config import merge_configs

from core.constants import RUN_FAILED_MESSAGE

from .agui import RuntimeContextLangGraphAGUIAgent
from .event_filter import SubagentEventFilter

if TYPE_CHECKING:
    from collections.abc import Awaitable
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
    """Drops ag-ui's log of the graph's own exception, which ``ainvoke_live`` re-raises for the executor to report;
    ag-ui's other failures are still logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        outcome = _driving_live_run.get()
        if outcome is None or record.msg != "LangGraph run failed" or not record.exc_info:
            return True
        return record.exc_info[1] is not outcome.error


logging.getLogger("ag_ui_langgraph.agent").addFilter(_DropReraisedRunFailure())


class LiveRunError(Exception):
    """The live stream ended without the graph returning or raising: ag-ui failed around it, or never ran it."""


async def publish_event(run_relay: RunRelay, event: BaseEvent) -> None:
    """Serialize one AG-UI event to the relay, stamped with its emit time.

    The chat client clocks a turn's reasoning segments from ``timestamp`` (epoch ms): a
    rejoining browser replays the whole run at once, so its own clock would measure every
    thought as instantaneous. Only this side knows when the event actually happened.
    """
    event.timestamp = event.timestamp or int(time.time() * 1000)
    await run_relay.publish_event(event.model_dump_json(by_alias=True, exclude_none=True))


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

    The graph's own outcome decides the call: its output is returned and its exception re-raised as is. The relay is
    best-effort: the first failure is logged and ends publishing for the run, which carries on. A RUN_ERROR is
    published with the generic message, since viewers other than the run's owner can read the stream.
    """
    outcome = _RootOutcome()
    publishing = await _attempt(run_relay, run_relay.clear())
    agui = RuntimeContextLangGraphAGUIAgent(
        name="DAIV",
        description="DAIV agent",
        graph=agent,
        config=merge_configs({"recursion_limit": recursion_limit, **config}, {"callbacks": [outcome]}),
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
    token = _driving_live_run.set(outcome)
    try:
        async with contextlib.aclosing(SubagentEventFilter().apply(agui.run(run_input))) as events:
            async for event in events:
                if isinstance(event, RunErrorEvent):
                    run_error = event.message
                    event = RunErrorEvent(type=EventType.RUN_ERROR, message=RUN_FAILED_MESSAGE, code="run_failed")
                publishing = publishing and await _attempt(run_relay, publish_event(run_relay, event))
    finally:
        _driving_live_run.reset(token)
        if publishing:
            await _attempt(run_relay, run_relay.publish_end())
    if outcome.error is not None:
        raise outcome.error
    if outcome.output is not None:
        return outcome.output
    raise LiveRunError(run_error or "graph produced no result")


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
    """The root run's output or error: ag-ui reports a failed graph only as a RUN_ERROR with the exception's text."""

    def __init__(self) -> None:
        self.output: dict[str, Any] | None = None
        self.error: BaseException | None = None

    async def on_chain_end(
        self, outputs: dict[str, Any], *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        if parent_run_id is None:
            self.output = outputs

    async def on_chain_error(
        self, error: BaseException, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> None:
        if parent_run_id is None:
            self.error = error
