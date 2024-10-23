from langchain_core.messages import AIMessage, ToolMessage

from automation.graphs.prebuilt import check_consecutive_tool_calls


class CheckConsecutiveToolCallsTest:
    def test_no_tool_calls(self):
        messages = [AIMessage("content")]
        assert check_consecutive_tool_calls(messages, "tool1") == 0

    def test_single_tool_call(self):
        messages = [AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}])]
        assert check_consecutive_tool_calls(messages, "tool1") == 1

    def test_multiple_consecutive_tool_calls(self):
        messages = [
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
            ToolMessage("content", tool_call_id="1"),
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
        ]
        assert check_consecutive_tool_calls(messages, "tool1") == 2

    def test_non_consecutive_tool_calls(self):
        messages = [
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
            ToolMessage("content", tool_call_id="1"),
            AIMessage("content"),
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
        ]
        assert check_consecutive_tool_calls(messages, "tool1") == 1

    def test_different_tool_calls(self):
        messages = [
            ToolMessage("content", tool_call_id="1"),
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
            ToolMessage("content", tool_call_id="1"),
            AIMessage("content", tool_calls=[{"name": "tool1", "args": {}, "id": "1"}]),
            ToolMessage("content", tool_call_id="1"),
            AIMessage("content", tool_calls=[{"name": "tool2", "args": {}, "id": "1"}]),
        ]
        assert check_consecutive_tool_calls(messages, "tool2") == 1

    def test_empty_messages(self):
        messages = []
        assert check_consecutive_tool_calls(messages, "tool1") == 0
