from __future__ import annotations

import pytest
from ag_ui.core.events import EventType, ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent

from chat.api.event_filter import SubagentEventFilter


async def to_list(stream):
    return [e async for e in stream]


async def gen(events):
    for e in events:
        yield e


def s(tcid: str, name: str = "read_file") -> ToolCallStartEvent:
    return ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=tcid, tool_call_name=name)


def a(tcid: str, delta: str) -> ToolCallArgsEvent:
    return ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=tcid, delta=delta)


def e(tcid: str) -> ToolCallEndEvent:
    return ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=tcid)


@pytest.mark.asyncio
async def test_natural_tool_call_lifecycle_passes_through_unchanged():
    filt = SubagentEventFilter()
    out = await to_list(filt.apply(gen([s("t1"), a("t1", '{"x":'), a("t1", "1}"), e("t1")])))
    assert [ev.type for ev in out] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]


@pytest.mark.asyncio
async def test_natural_reemit_after_end_is_dropped():
    filt = SubagentEventFilter()
    seq = [
        s("t1"),
        a("t1", '{"x":1}'),
        e("t1"),
        # OnToolEnd re-emit — must be dropped
        s("t1"),
        a("t1", '{"x":1}'),
        e("t1"),
    ]
    out = await to_list(filt.apply(gen(seq)))
    assert [ev.type for ev in out] == [EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END]
