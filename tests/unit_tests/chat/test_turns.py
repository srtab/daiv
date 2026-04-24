from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

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
