"""Tests for ``SubagentEventFilter``.

Mirrors the live ag_ui_langgraph stream in three respects: top-level events
carry an empty/single-segment ``langgraph_checkpoint_ns``; nested subagent
events carry a ``"<parent>:UUID|<inner>:UUID"`` ns; and the parent's ``task``
TOOL_CALL_START is delivered late (via the OnToolEnd re-emit) *after* the
subagent has already streamed events.
"""

from ag_ui.core.events import (
    EventType,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from chat.api.event_filter import SubagentEventFilter


def _ev(klass, *, ns: str = "", **kwargs):
    return klass(raw_event={"metadata": {"langgraph_checkpoint_ns": ns}}, **kwargs)


async def _drain(stream):
    return [ev async for ev in stream]


async def _aiter(items):
    for it in items:
        yield it


def _filter(events):
    return SubagentEventFilter().apply(_aiter(events))


async def test_filter_drops_nested_subagent_events():
    events = [
        _ev(TextMessageStartEvent, ns="model:abc", message_id="m1", role="assistant"),
        _ev(TextMessageContentEvent, ns="model:abc", message_id="m1", delta="hi"),
        _ev(TextMessageStartEvent, ns="tools:par|model:sub", message_id="m2", role="assistant"),
        _ev(TextMessageContentEvent, ns="tools:par|model:sub", message_id="m2", delta="bleed"),
    ]
    out = await _drain(_filter(events))
    # Only the two top-level (ns without ``|``) events survive.
    assert [e.type for e in out] == [EventType.TEXT_MESSAGE_START, EventType.TEXT_MESSAGE_CONTENT]
    assert all("|" not in (e.raw_event or {}).get("metadata", {}).get("langgraph_checkpoint_ns", "") for e in out)


async def test_filter_synthesizes_task_start_before_first_nested_event():
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {"type": "human", "content": "go"},
                {
                    "type": "ai",
                    "content": "Now launching subagent.",
                    "tool_calls": [{"id": "tc-task-1", "name": "task", "args": {"subagent_type": "explore"}}],
                },
            ]
        },
    )
    nested = _ev(TextMessageStartEvent, ns="tools:par|model:sub", message_id="m1", role="assistant")
    out = await _drain(_filter([snapshot, nested]))
    types = [e.type for e in out]
    assert types == [
        EventType.STATE_SNAPSHOT,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
    ]
    start = out[1]
    assert start.tool_call_id == "tc-task-1"
    assert start.tool_call_name == "task"
    args = out[2]
    assert args.tool_call_id == "tc-task-1"
    assert "explore" in args.delta


async def test_filter_drops_late_reemit_for_synthesized_task():
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {"type": "ai", "content": "...", "tool_calls": [{"id": "tc-task-1", "name": "task", "args": {"x": 1}}]}
            ]
        },
    )
    nested = _ev(TextMessageStartEvent, ns="tools:par|model:sub", message_id="m1", role="assistant")
    # The OnToolEnd re-emit re-issues START/ARGS/END for the *same* tool_call_id
    # at the top level, after the subagent has finished.
    late_start = _ev(ToolCallStartEvent, ns="tools:par", tool_call_id="tc-task-1", tool_call_name="task")
    late_args = _ev(ToolCallArgsEvent, ns="tools:par", tool_call_id="tc-task-1", delta='{"x":1}')
    late_end = _ev(ToolCallEndEvent, ns="tools:par", tool_call_id="tc-task-1")
    result = _ev(ToolCallResultEvent, ns="", tool_call_id="tc-task-1", message_id="r1", content="done")

    out = await _drain(_filter([snapshot, nested, late_start, late_args, late_end, result]))
    types = [e.type for e in out]
    # Synthetic START/ARGS/END (3) + STATE_SNAPSHOT + RESULT — no late re-emit.
    assert types == [
        EventType.STATE_SNAPSHOT,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
    ]


async def test_filter_does_not_synthesize_for_already_emitted_task():
    snap1 = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-1", "name": "task", "args": {}}]}]},
    )
    nested1 = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    # A *second* top-level snapshot rebroadcasts the same already-streamed
    # tool_call. We must not re-emit synthetic events for it.
    snap2 = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-1", "name": "task", "args": {}}]}]},
    )
    nested2 = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m2", role="assistant")
    out = await _drain(_filter([snap1, nested1, snap2, nested2]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert len(starts) == 1


async def test_filter_passes_non_task_tool_calls_through():
    # A plain ``read_file`` tool call must surface unchanged — only ``task``
    # tool_calls are intercepted by the synthesize/dedupe path.
    events = [
        _ev(ToolCallStartEvent, ns="tools:p", tool_call_id="tc-rf", tool_call_name="read_file"),
        _ev(ToolCallArgsEvent, ns="tools:p", tool_call_id="tc-rf", delta='{"path":"/a"}'),
        _ev(ToolCallEndEvent, ns="tools:p", tool_call_id="tc-rf"),
        _ev(ToolCallResultEvent, ns="", tool_call_id="tc-rf", message_id="r", content="contents"),
    ]
    out = await _drain(_filter(events))
    assert [e.type for e in out] == [
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
    ]


async def test_filter_handles_parallel_task_tool_calls():
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {
                    "type": "ai",
                    "tool_calls": [
                        {"id": "tc-a", "name": "task", "args": {"subagent_type": "explore"}},
                        {"id": "tc-b", "name": "task", "args": {"subagent_type": "general-purpose"}},
                    ],
                }
            ]
        },
    )
    nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    out = await _drain(_filter([snapshot, nested]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert {s.tool_call_id for s in starts} == {"tc-a", "tc-b"}


async def test_filter_skips_args_event_when_args_are_empty():
    # An empty-dict args field should not produce a useless TOOL_CALL_ARGS
    # frame carrying ``"{}"`` — the chat UI would render it as a stray empty
    # delta. Tested for both ``{}`` and ``""``.
    for empty_args in ({}, ""):
        snapshot = _ev(
            StateSnapshotEvent,
            ns="",
            snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-x", "name": "task", "args": empty_args}]}]},
        )
        nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
        out = await _drain(_filter([snapshot, nested]))
        assert [e.type for e in out] == [EventType.STATE_SNAPSHOT, EventType.TOOL_CALL_START, EventType.TOOL_CALL_END]


async def test_filter_handles_langchain_message_objects_in_snapshot():
    # In the live ag_ui_langgraph stream the snapshot's ``messages`` arrive as
    # LangChain BaseMessage instances, not dicts (the AGUI encoder serializes
    # them later, but our filter sits in front of the encoder).
    from langchain_core.messages import AIMessage, HumanMessage

    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                HumanMessage(content="hi", id="u1"),
                AIMessage(
                    content="launching", id="m1", tool_calls=[{"id": "tc-task-1", "name": "task", "args": {"x": 1}}]
                ),
            ]
        },
    )
    nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    out = await _drain(_filter([snapshot, nested]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [s.tool_call_id for s in starts] == ["tc-task-1"]


async def test_filter_passes_late_reemit_through_when_no_nested_event_fired():
    # If a subagent returns immediately (or upstream skips emitting nested
    # events for some other reason), synthesis never fires. The late
    # OnToolEnd re-emit then becomes the user-visible TOOL_CALL_START — it
    # must NOT be deduped because nothing was synthesized for that tcid.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-fast", "name": "task", "args": {"x": 1}}]}]},
    )
    late_start = _ev(ToolCallStartEvent, ns="tools:p", tool_call_id="tc-fast", tool_call_name="task")
    late_args = _ev(ToolCallArgsEvent, ns="tools:p", tool_call_id="tc-fast", delta='{"x":1}')
    late_end = _ev(ToolCallEndEvent, ns="tools:p", tool_call_id="tc-fast")
    result = _ev(ToolCallResultEvent, ns="", tool_call_id="tc-fast", message_id="r", content="done")
    out = await _drain(_filter([snapshot, late_start, late_args, late_end, result]))
    assert [e.type for e in out] == [
        EventType.STATE_SNAPSHOT,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
    ]


async def test_filter_passes_tool_call_result_for_synthesized_task():
    # Isolated regression test for the docstring's "RESULT flows through
    # untouched" claim. Locks in that the dedup logic only targets the
    # START/ARGS/END trio, never RESULT.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-r", "name": "task", "args": {}}]}]},
    )
    nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    result = _ev(ToolCallResultEvent, ns="", tool_call_id="tc-r", message_id="r", content="payload")
    out = await _drain(_filter([snapshot, nested, result]))
    [tcr] = [e for e in out if e.type == EventType.TOOL_CALL_RESULT]
    assert tcr.tool_call_id == "tc-r"
    assert tcr.content == "payload"


async def test_filter_synthesizes_args_string_passthrough_without_reencoding():
    # ``args`` arrives as a partial JSON string when the parent's chat model
    # streams the tool_call mid-flight. Re-encoding via ``json.dumps`` would
    # double-quote it; the filter must pass strings through verbatim.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-s", "name": "task", "args": '{"q":"x"}'}]}]},
    )
    nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    out = await _drain(_filter([snapshot, nested]))
    [args_ev] = [e for e in out if e.type == EventType.TOOL_CALL_ARGS]
    assert args_ev.delta == '{"q":"x"}'


async def test_filter_only_synthesizes_latest_ai_message_tool_calls():
    # Older AI messages in the snapshot have already been streamed (their
    # tool_calls are in ``task_calls`` from a prior snapshot). The filter
    # must only consider the latest AIMessage so a snapshot reissuing old
    # history doesn't re-trigger synthesis for already-completed tasks.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {"type": "ai", "tool_calls": [{"id": "tc-old", "name": "task", "args": {}}]},
                {"type": "tool", "tool_call_id": "tc-old", "content": "done"},
                {"type": "ai", "tool_calls": [{"id": "tc-new", "name": "task", "args": {}}]},
            ]
        },
    )
    nested = _ev(TextMessageStartEvent, ns="tools:p|model:s", message_id="m", role="assistant")
    out = await _drain(_filter([snapshot, nested]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [s.tool_call_id for s in starts] == ["tc-new"]
