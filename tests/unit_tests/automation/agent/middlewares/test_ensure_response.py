from uuid import uuid4

from langchain_core.messages import AIMessage

from automation.agent.middlewares.ensure_response import NO_OP_TOOL_NAME, ensure_non_empty_response


class TestEnsureNonEmptyResponse:
    async def test_returns_none_when_response_has_content(self):
        state = {"messages": [AIMessage(content="Hello, I can help with that.")]}
        result = await ensure_non_empty_response.aafter_model(state, None)
        assert result is None

    async def test_returns_none_when_response_has_tool_calls(self):
        tc_id = str(uuid4())
        msg = AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "foo.py"}, "id": tc_id}])
        state = {"messages": [msg]}
        result = await ensure_non_empty_response.aafter_model(state, None)
        assert result is None

    async def test_injects_no_op_when_response_is_empty(self):
        msg = AIMessage(content="")
        state = {"messages": [msg]}
        result = await ensure_non_empty_response.aafter_model(state, None)

        assert result is not None
        assert "messages" in result
        assert len(result["messages"]) == 2

        ai_msg = result["messages"][0]
        assert len(ai_msg.tool_calls) == 1
        assert ai_msg.tool_calls[0]["name"] == NO_OP_TOOL_NAME

        tool_msg = result["messages"][1]
        assert tool_msg.name == NO_OP_TOOL_NAME
        assert "empty" in tool_msg.content.lower()

    async def test_injects_no_op_when_response_has_no_content_and_no_tool_calls(self):
        msg = AIMessage(content="", tool_calls=[])
        state = {"messages": [msg]}
        result = await ensure_non_empty_response.aafter_model(state, None)

        assert result is not None
        assert result["messages"][0].tool_calls[0]["name"] == NO_OP_TOOL_NAME

    async def test_does_not_mutate_original_message(self):
        msg = AIMessage(content="", id="original-id")
        state = {"messages": [msg]}
        result = await ensure_non_empty_response.aafter_model(state, None)

        assert result is not None
        # Original message must remain untouched
        assert msg.tool_calls == []
        # Patched message is a copy with the same id (for LangGraph's reducer to replace it)
        patched_msg = result["messages"][0]
        assert patched_msg.id == "original-id"
        assert patched_msg is not msg
        assert len(patched_msg.tool_calls) == 1
