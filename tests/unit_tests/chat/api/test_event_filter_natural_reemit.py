from __future__ import annotations

from ag_ui.core.events import EventType, StateSnapshotEvent, ToolCallArgsEvent, ToolCallEndEvent, ToolCallStartEvent

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


async def test_natural_tool_call_lifecycle_passes_through_unchanged():
    filt = SubagentEventFilter()
    out = await to_list(filt.apply(gen([s("t1"), a("t1", '{"x":'), a("t1", "1}"), e("t1")])))
    assert [ev.type for ev in out] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]


async def test_natural_reemit_after_end_is_dropped():
    filt = SubagentEventFilter()
    seq = [s("t1"), a("t1", '{"x":1}'), e("t1"), s("t1"), a("t1", '{"x":1}'), e("t1")]
    out = await to_list(filt.apply(gen(seq)))
    assert [ev.type for ev in out] == [EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END]


async def test_args_reemit_for_natural_tcid_is_dropped_independently_of_index():
    """Re-emitted ARGS without ``tool_call_chunks`` must still be dropped when
    the lifecycle has already closed naturally — ``_is_misrouted_arg`` returns
    False (no chunk metadata), so the dedup against ``_natural_ended`` is what
    catches it."""
    filt = SubagentEventFilter()
    seq = [s("t1"), a("t1", '{"x":1}'), e("t1"), a("t1", '{"x":1}')]
    out = await to_list(filt.apply(gen(seq)))
    assert [ev.type for ev in out] == [EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END]


async def test_orphan_end_without_matching_start_is_yielded_but_does_not_record_natural_ended():
    """If a TOOL_CALL_END arrives whose tcid never had a TOOL_CALL_START
    (theoretical upstream regression), it should still pass through, and a
    subsequent re-emit must NOT be dropped (we don't have proof the lifecycle
    closed correctly)."""
    filt = SubagentEventFilter()
    out = await to_list(filt.apply(gen([e("t1"), s("t1"), e("t1")])))
    assert [ev.type for ev in out] == [EventType.TOOL_CALL_END, EventType.TOOL_CALL_START, EventType.TOOL_CALL_END]


async def test_synthesized_drops_take_precedence_over_natural_ended():
    """A tcid that arrives via STATE_SNAPSHOT first is synthesized; subsequent
    natural Start/Args/End must be dropped via ``_synthesized``, not ``_natural_ended``."""
    snap = StateSnapshotEvent(
        type=EventType.STATE_SNAPSHOT,
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "t1", "name": "read_file", "args": {"x": 1}}]}]},
    )
    filt = SubagentEventFilter()
    out = await to_list(filt.apply(gen([snap, s("t1"), a("t1", '{"x":1}'), e("t1")])))
    types = [ev.type for ev in out]
    assert types == [
        EventType.STATE_SNAPSHOT,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]
    assert "t1" in filt._synthesized
    assert "t1" not in filt._natural_started
