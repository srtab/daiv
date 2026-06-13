from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from memory.transcript import serialize_transcript


def test_serializes_roles_text_and_tool_calls():
    messages = [
        HumanMessage(content="Fix the failing test in foo.py"),
        AIMessage(
            content="Looking at it.", tool_calls=[{"name": "read_file", "args": {"path": "foo.py"}, "id": "tc-1"}]
        ),
        ToolMessage(content="def foo(): ...", tool_call_id="tc-1", name="read_file"),
    ]
    transcript = serialize_transcript(messages)
    assert "[human] Fix the failing test in foo.py" in transcript
    assert "[ai] Looking at it." in transcript
    assert "read_file" in transcript
    assert "[tool:read_file] def foo(): ..." in transcript


def test_truncates_long_tool_outputs():
    messages = [ToolMessage(content="x" * 50_000, tool_call_id="tc-1", name="bash")]
    transcript = serialize_transcript(messages)
    assert len(transcript) < 5_000
    assert "truncated" in transcript


def test_caps_total_size_keeping_head_and_tail():
    messages = [HumanMessage(content=f"message number {i} " + "y" * 500) for i in range(500)]
    transcript = serialize_transcript(messages, max_chars=10_000)
    assert len(transcript) < 11_000
    assert "message number 0" in transcript, "head (task definition) must survive"
    assert "message number 499" in transcript, "tail (outcome) must survive"
    assert "elided" in transcript
