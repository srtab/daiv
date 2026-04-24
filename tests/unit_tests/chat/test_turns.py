from __future__ import annotations

from langchain_core.messages import HumanMessage

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
