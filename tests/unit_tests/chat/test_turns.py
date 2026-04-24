from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chat.turns import build_turns


def test_build_turns_empty_list_returns_empty():
    assert build_turns([]) == []


def test_build_turns_human_string_content_single_text_segment():
    m = HumanMessage(content="hello world", id="h-1")
    result = build_turns([m])
    assert result == [{"id": "h-1", "role": "user", "segments": [{"type": "text", "content": "hello world"}]}]


def test_build_turns_human_list_content_joins_text_blocks():
    m = HumanMessage(content=[{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}], id="h-2")
    result = build_turns([m])
    assert result[0]["segments"] == [{"type": "text", "content": "hi there"}]


def test_build_turns_ai_string_with_tool_calls_emits_text_then_tool_segments():
    m = AIMessage(
        content="Let me check the README.",
        id="a-1",
        tool_calls=[
            {"id": "tc-1", "name": "read_file", "args": {"path": "README.md"}},
            {"id": "tc-2", "name": "grep", "args": {"pattern": "TODO"}},
        ],
    )
    result = build_turns([m])
    assert len(result) == 1
    turn = result[0]
    assert turn["role"] == "assistant"
    assert turn["id"] == "a-1"
    assert turn["segments"] == [
        {"type": "text", "content": "Let me check the README."},
        {
            "type": "tool_call",
            "id": "tc-1",
            "name": "read_file",
            "args": '{"path": "README.md"}',
            "result": None,
            "status": "done",
        },
        {
            "type": "tool_call",
            "id": "tc-2",
            "name": "grep",
            "args": '{"pattern": "TODO"}',
            "result": None,
            "status": "done",
        },
    ]


def test_build_turns_ai_empty_content_omits_text_segment():
    m = AIMessage(content="", id="a-2", tool_calls=[{"id": "tc-3", "name": "ls", "args": {"path": "/"}}])
    result = build_turns([m])
    assert result[0]["segments"] == [
        {"type": "tool_call", "id": "tc-3", "name": "ls", "args": '{"path": "/"}', "result": None, "status": "done"}
    ]


def test_build_turns_ai_list_content_preserves_block_interleaving():
    m = AIMessage(
        content=[
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "tc-a", "name": "read_file", "input": {"path": "a.py"}},
            {"type": "text", "text": "Now I'll search."},
            {"type": "tool_use", "id": "tc-b", "name": "grep", "input": {"pattern": "x"}},
        ],
        id="a-3",
        tool_calls=[
            {"id": "tc-a", "name": "read_file", "args": {"path": "a.py"}},
            {"id": "tc-b", "name": "grep", "args": {"pattern": "x"}},
        ],
    )
    result = build_turns([m])
    segments = result[0]["segments"]
    assert [s["type"] for s in segments] == ["text", "tool_call", "text", "tool_call"]
    assert segments[0]["content"] == "Let me look."
    assert segments[1]["id"] == "tc-a"
    assert segments[1]["name"] == "read_file"
    assert segments[2]["content"] == "Now I'll search."
    assert segments[3]["id"] == "tc-b"


def test_build_turns_ai_list_content_tool_use_without_matching_tool_call_still_emitted():
    m = AIMessage(
        content=[{"type": "tool_use", "id": "tc-orphan", "name": "custom", "input": {"k": "v"}}],
        id="a-4",
        tool_calls=[],
    )
    result = build_turns([m])
    assert result[0]["segments"] == [
        {
            "type": "tool_call",
            "id": "tc-orphan",
            "name": "custom",
            "args": '{"k": "v"}',
            "result": None,
            "status": "done",
        }
    ]


def test_build_turns_tool_message_result_attaches_to_matching_tool_call():
    ai = AIMessage(
        content="Let me check.", id="a-5", tool_calls=[{"id": "tc-x", "name": "read_file", "args": {"path": "x.py"}}]
    )
    tool = ToolMessage(content="file contents", tool_call_id="tc-x", id="t-1")
    result = build_turns([ai, tool])
    assert len(result) == 1  # ToolMessages do not create their own turn
    tool_seg = result[0]["segments"][1]
    assert tool_seg["result"] == "file contents"
    assert tool_seg["status"] == "done"


def test_build_turns_tool_message_list_content_joins_text_blocks():
    ai = AIMessage(content="", id="a-6", tool_calls=[{"id": "tc-y", "name": "grep", "args": {"pattern": "x"}}])
    tool = ToolMessage(
        content=[{"type": "text", "text": "line-a"}, {"type": "text", "text": "line-b"}], tool_call_id="tc-y", id="t-2"
    )
    result = build_turns([ai, tool])
    assert result[0]["segments"][0]["result"] == "line-a\nline-b"


def test_build_turns_orphan_tool_message_is_dropped_with_warning(caplog):
    tool = ToolMessage(content="orphan", tool_call_id="tc-missing", id="t-3")
    with caplog.at_level("WARNING", logger="daiv.chat"):
        result = build_turns([tool])
    assert result == []
    assert any("tc-missing" in rec.message for rec in caplog.records)


def test_build_turns_mixed_order_human_ai_tool_ai_tool_human():
    msgs = [
        HumanMessage(content="first prompt", id="h-1"),
        AIMessage(content="ok", id="a-1", tool_calls=[{"id": "tc-1", "name": "read_file", "args": {"path": "a"}}]),
        ToolMessage(content="result-1", tool_call_id="tc-1", id="t-1"),
        AIMessage(content="next", id="a-2", tool_calls=[{"id": "tc-2", "name": "grep", "args": {"pattern": "z"}}]),
        ToolMessage(content="result-2", tool_call_id="tc-2", id="t-2"),
        HumanMessage(content="second prompt", id="h-2"),
    ]
    result = build_turns(msgs)
    assert [t["role"] for t in result] == ["user", "assistant", "assistant", "user"]
    assert result[1]["segments"][1]["result"] == "result-1"
    assert result[2]["segments"][1]["result"] == "result-2"
