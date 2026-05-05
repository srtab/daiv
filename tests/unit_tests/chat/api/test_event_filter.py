"""Tests for ``SubagentEventFilter``.

Mirrors the live ag_ui_langgraph stream in three respects: top-level events
carry an empty/single-segment ``langgraph_checkpoint_ns``; nested subagent
events carry a ``"<parent>:UUID|<inner>:UUID"`` ns; and the parent's ``task``
TOOL_CALL_START is delivered late (via the OnToolEnd re-emit) *after* the
subagent has already streamed events.
"""

from ag_ui.core.events import (
    EventType,
    RawEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from chat.api.event_filter import SubagentEventFilter


def _ev(klass, *, ns: str = "", chunk: dict | None = None, **kwargs):
    raw: dict = {"metadata": {"langgraph_checkpoint_ns": ns}}
    if chunk is not None:
        raw["data"] = {"chunk": chunk}
    return klass(raw_event=raw, **kwargs)


def _chat_model_end(*, output: object | None, ns: str = "") -> RawEvent:
    """Mirror ag_ui_langgraph's ``on_chat_model_end`` shape: payload on ``event``
    (not ``raw_event``), AIMessage output under ``data.output``.
    """
    payload: dict = {
        "event": "on_chat_model_end",
        "metadata": {"langgraph_checkpoint_ns": ns},
        "data": {"output": output},
    }
    return RawEvent(event=payload)


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


async def test_filter_drops_misrouted_args_for_sibling_tool_calls():
    # ag_ui_langgraph misroutes streamed arg deltas: when the LLM moves on
    # from the first tool_call to a sibling, subsequent ARGS chunks still
    # carry the *first* tool's id but the underlying chunk's
    # ``tool_call_chunks[0].index`` points at the sibling. Drop those so the
    # first tool's args don't get a concatenated JSON blob; the sibling is
    # recovered via STATE_SNAPSHOT synthesis.
    natural_start = _ev(
        ToolCallStartEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "id": "tc1", "name": "read_file", "args": ""}]},
        tool_call_id="tc1",
        tool_call_name="read_file",
    )
    own_arg = _ev(
        ToolCallArgsEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "args": '{"path":"a.py"}'}]},
        tool_call_id="tc1",
        delta='{"path":"a.py"}',
    )
    sibling_arg = _ev(
        ToolCallArgsEvent,
        ns="model:m",
        # chunk index 1 → belongs to the second sibling tool_call; the event's
        # tool_call_id is wrong (still tc1) because ag_ui_langgraph reuses
        # current_stream's id.
        chunk={"tool_call_chunks": [{"index": 1, "args": '{"path":"b.py"}'}]},
        tool_call_id="tc1",
        delta='{"path":"b.py"}',
    )
    out = await _drain(_filter([natural_start, own_arg, sibling_arg]))
    assert [e.type for e in out] == [EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS]
    assert out[1].delta == '{"path":"a.py"}'


async def test_filter_synthesizes_sibling_tool_calls_after_natural_first():
    # The realistic multi-tool-call sequence: tc1 streams naturally (its
    # TOOL_CALL_START + correct-index ARGS pass through), tc2's args are
    # misrouted-and-dropped, then STATE_SNAPSHOT arrives with both tcids.
    # tc1 must NOT be re-synthesized (it's already on the wire); tc2 MUST be
    # synthesized so its segment exists when its TOOL_CALL_RESULT arrives.
    natural_start = _ev(
        ToolCallStartEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "id": "tc1", "name": "read_file", "args": ""}]},
        tool_call_id="tc1",
        tool_call_name="read_file",
    )
    own_arg = _ev(
        ToolCallArgsEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "args": '{"path":"a.py"}'}]},
        tool_call_id="tc1",
        delta='{"path":"a.py"}',
    )
    misrouted = _ev(
        ToolCallArgsEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 1, "args": '{"path":"b.py"}'}]},
        tool_call_id="tc1",
        delta='{"path":"b.py"}',
    )
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {
                    "type": "ai",
                    "tool_calls": [
                        {"id": "tc1", "name": "read_file", "args": {"path": "a.py"}},
                        {"id": "tc2", "name": "read_file", "args": {"path": "b.py"}},
                    ],
                }
            ]
        },
    )
    result_2 = _ev(ToolCallResultEvent, ns="", tool_call_id="tc2", message_id="r2", content="contents-b")
    out = await _drain(_filter([natural_start, own_arg, misrouted, snapshot, result_2]))

    starts = [(e.tool_call_id, getattr(e, "tool_call_name", None)) for e in out if e.type == EventType.TOOL_CALL_START]
    # tc1 from natural stream + tc2 synthesized — tc1 NOT duplicated.
    assert starts == [("tc1", "read_file"), ("tc2", "read_file")]
    args = [(e.tool_call_id, e.delta) for e in out if e.type == EventType.TOOL_CALL_ARGS]
    # tc1's own (index 0) arg + synthesized tc2 args; misrouted index-1 dropped.
    assert args == [("tc1", '{"path":"a.py"}'), ("tc2", '{"path": "b.py"}')]


async def test_filter_synthesizes_for_non_task_tool_calls_dropped_by_ag_ui():
    # When ag_ui_langgraph drops the natural TOOL_CALL_START on a
    # text→tool_call transition (the same code path that drops ``task``
    # starts), we still synthesize from STATE_SNAPSHOT regardless of the
    # tool name — non-``task`` calls should also get a segment so their
    # RESULT can find one.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {
                    "type": "ai",
                    "content": "Looking up the file.",
                    "tool_calls": [{"id": "tc-rf", "name": "read_file", "args": {"path": "x.py"}}],
                }
            ]
        },
    )
    out = await _drain(_filter([snapshot]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [(s.tool_call_id, s.tool_call_name) for s in starts] == [("tc-rf", "read_file")]


async def test_filter_passes_args_with_malformed_chunk_shapes_through():
    # Malformed/missing chunk shapes must not crash and must not be
    # misclassified as misrouted — the event passes through unchanged so the
    # natural arg streaming for the first tool_call survives upstream
    # changes.
    cases = [
        # No data key.
        {"metadata": {"langgraph_checkpoint_ns": ""}},
        # data.chunk is None.
        {"metadata": {"langgraph_checkpoint_ns": ""}, "data": {"chunk": None}},
        # tool_call_chunks missing.
        {"metadata": {"langgraph_checkpoint_ns": ""}, "data": {"chunk": {}}},
        # tool_call_chunks is empty.
        {"metadata": {"langgraph_checkpoint_ns": ""}, "data": {"chunk": {"tool_call_chunks": []}}},
        # First chunk lacks index.
        {"metadata": {"langgraph_checkpoint_ns": ""}, "data": {"chunk": {"tool_call_chunks": [{"args": "x"}]}}},
        # First chunk has non-int index.
        {
            "metadata": {"langgraph_checkpoint_ns": ""},
            "data": {"chunk": {"tool_call_chunks": [{"index": "1", "args": "x"}]}},
        },
    ]
    for raw in cases:
        ev = ToolCallArgsEvent(raw_event=raw, tool_call_id="tc1", delta="x")
        out = await _drain(_filter([ev]))
        assert [e.type for e in out] == [EventType.TOOL_CALL_ARGS], f"failed for raw={raw}"


async def test_filter_drops_misrouted_args_for_already_synthesized_tcid():
    # If a tcid was synthesized (not naturally started) AND a misrouted ARGS
    # arrives carrying it, both drop conditions hold. Lock in that the event
    # is dropped exactly once — order of the two checks must not matter.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc-s", "name": "task", "args": {"x": 1}}]}]},
    )
    misrouted = _ev(
        ToolCallArgsEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 1, "args": "y"}]},
        tool_call_id="tc-s",
        delta="y",
    )
    out = await _drain(_filter([snapshot, misrouted]))
    args_events = [e for e in out if e.type == EventType.TOOL_CALL_ARGS]
    # Only the synthesized ARGS for tc-s — the misrouted one is dropped.
    assert [a.delta for a in args_events] == ['{"x": 1}']


async def test_filter_synthesizes_from_chat_model_end_for_parallel_siblings():
    # Snapshot synthesis can't catch this case — the tools node's snapshots
    # only carry the per-tool ToolMessages, not the parent AIMessage.
    natural_start = _ev(
        ToolCallStartEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "id": "tc1", "name": "edit_file", "args": ""}]},
        tool_call_id="tc1",
        tool_call_name="edit_file",
    )
    natural_end = _ev(ToolCallEndEvent, ns="model:m", tool_call_id="tc1")
    model_end = _chat_model_end(
        output={
            "type": "ai",
            "tool_calls": [
                {"id": "tc1", "name": "edit_file", "args": {"path": "a.md"}},
                {"id": "tc2", "name": "edit_file", "args": {"path": "b.md"}},
                {"id": "tc3", "name": "edit_file", "args": {"path": "c.md"}},
            ],
        }
    )
    out = await _drain(_filter([natural_start, natural_end, model_end]))
    starts = [(e.tool_call_id, e.tool_call_name) for e in out if e.type == EventType.TOOL_CALL_START]
    # tc1 from the natural stream + tc2 and tc3 synthesized from on_chat_model_end.
    assert starts == [("tc1", "edit_file"), ("tc2", "edit_file"), ("tc3", "edit_file")]
    args = [(e.tool_call_id, e.delta) for e in out if e.type == EventType.TOOL_CALL_ARGS]
    # Synthesized siblings carry their finalized args; the natural tc1 had no
    # ARGS event in this minimal sequence so it doesn't appear here.
    assert args == [("tc2", '{"path": "b.md"}'), ("tc3", '{"path": "c.md"}')]


async def test_filter_chat_model_end_dedupes_late_reemit():
    model_end = _chat_model_end(
        output={"type": "ai", "tool_calls": [{"id": "tc-x", "name": "edit_file", "args": {"k": 1}}]}
    )
    late_start = _ev(ToolCallStartEvent, ns="tools:p", tool_call_id="tc-x", tool_call_name="edit_file")
    late_args = _ev(ToolCallArgsEvent, ns="tools:p", tool_call_id="tc-x", delta='{"k":1}')
    late_end = _ev(ToolCallEndEvent, ns="tools:p", tool_call_id="tc-x")
    result = _ev(ToolCallResultEvent, ns="", tool_call_id="tc-x", message_id="r", content="ok")

    out = await _drain(_filter([model_end, late_start, late_args, late_end, result]))
    types = [e.type for e in out]
    # RAW (passes through) + synthesized START/ARGS/END + RESULT — late re-emit dropped.
    assert types == [
        EventType.RAW,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
    ]


async def test_filter_chat_model_end_skips_naturally_started_tcid():
    natural = _ev(
        ToolCallStartEvent,
        ns="model:m",
        chunk={"tool_call_chunks": [{"index": 0, "id": "tc1", "name": "edit_file", "args": ""}]},
        tool_call_id="tc1",
        tool_call_name="edit_file",
    )
    model_end = _chat_model_end(
        output={"type": "ai", "tool_calls": [{"id": "tc1", "name": "edit_file", "args": {"x": 1}}]}
    )
    out = await _drain(_filter([natural, model_end]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [s.tool_call_id for s in starts] == ["tc1"]


async def test_filter_chat_model_end_skips_already_synthesized_tcid():
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={"messages": [{"type": "ai", "tool_calls": [{"id": "tc1", "name": "edit_file", "args": {}}]}]},
    )
    model_end = _chat_model_end(output={"type": "ai", "tool_calls": [{"id": "tc1", "name": "edit_file", "args": {}}]})
    out = await _drain(_filter([snapshot, model_end]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [s.tool_call_id for s in starts] == ["tc1"]


async def test_filter_passes_unrelated_raw_events_through():
    other_raw = RawEvent(event={"event": "on_tool_start", "metadata": {"langgraph_checkpoint_ns": ""}})
    out = await _drain(_filter([other_raw]))
    assert [e.type for e in out] == [EventType.RAW]


async def test_filter_drops_nested_chat_model_end_raw():
    # Synthesizing a subagent's tool_calls into the parent stream would create
    # phantom cards for tools the parent never invoked.
    nested_model_end = _chat_model_end(
        ns="tools:p|model:s",
        output={"type": "ai", "tool_calls": [{"id": "tc-nested", "name": "edit_file", "args": {}}]},
    )
    out = await _drain(_filter([nested_model_end]))
    assert out == []


async def test_filter_chat_model_end_handles_malformed_payloads():
    cases = [
        # No data key at all.
        RawEvent(event={"event": "on_chat_model_end", "metadata": {"langgraph_checkpoint_ns": ""}}),
        # data.output is None.
        RawEvent(
            event={"event": "on_chat_model_end", "metadata": {"langgraph_checkpoint_ns": ""}, "data": {"output": None}}
        ),
        # tool_calls missing on the output.
        RawEvent(
            event={
                "event": "on_chat_model_end",
                "metadata": {"langgraph_checkpoint_ns": ""},
                "data": {"output": {"type": "ai", "content": "no tools"}},
            }
        ),
        # tool_call entry missing id.
        RawEvent(
            event={
                "event": "on_chat_model_end",
                "metadata": {"langgraph_checkpoint_ns": ""},
                "data": {"output": {"type": "ai", "tool_calls": [{"name": "edit_file", "args": {}}]}},
            }
        ),
    ]
    for ev in cases:
        out = await _drain(_filter([ev]))
        assert [e.type for e in out] == [EventType.RAW], f"failed for raw={ev.event}"


async def test_filter_chat_model_end_handles_aimessage_object():
    # In the live stream output arrives as an AIMessage instance (this filter
    # runs in front of the AGUI encoder that serializes to dicts).
    from langchain_core.messages import AIMessage

    output = AIMessage(content="", id="m1", tool_calls=[{"id": "tc-obj", "name": "edit_file", "args": {"k": 1}}])
    model_end = _chat_model_end(output=output)
    out = await _drain(_filter([model_end]))
    starts = [e for e in out if e.type == EventType.TOOL_CALL_START]
    assert [(s.tool_call_id, s.tool_call_name) for s in starts] == [("tc-obj", "edit_file")]


async def test_filter_skips_synthesis_when_latest_ai_has_no_tool_calls():
    # _iter_latest_tool_calls returns after the first AI message even if its
    # tool_calls is empty — older AI messages' tool_calls were already
    # emitted on prior snapshots and must not re-synthesize.
    snapshot = _ev(
        StateSnapshotEvent,
        ns="",
        snapshot={
            "messages": [
                {"type": "ai", "tool_calls": [{"id": "tc-old", "name": "task", "args": {}}]},
                {"type": "tool", "tool_call_id": "tc-old", "content": "done"},
                {"type": "ai", "content": "all done", "tool_calls": []},
            ]
        },
    )
    out = await _drain(_filter([snapshot]))
    assert [e.type for e in out] == [EventType.STATE_SNAPSHOT]
