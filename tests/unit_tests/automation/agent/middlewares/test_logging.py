import logging
from unittest.mock import Mock


class TestToolCallLoggingMiddleware:
    async def test_logs_tool_call(self, caplog):
        from langchain_core.messages import ToolMessage
        from langgraph.prebuilt.tool_node import ToolCallRequest

        from automation.agent.middlewares.logging import ToolCallLoggingMiddleware

        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(req: ToolCallRequest):
            return ToolMessage(content="ok", tool_call_id=req.tool_call["id"], name=req.tool_call["name"])

        result = await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)
        assert isinstance(result, ToolMessage)
        assert result.content == "ok"

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call (id=call_1" in m for m in messages)

    async def test_logs_tool_call_exception_and_reraises(self, caplog):
        from langgraph.prebuilt.tool_node import ToolCallRequest

        from automation.agent.middlewares.logging import ToolCallLoggingMiddleware

        caplog.set_level(logging.INFO, logger="daiv.tools")

        request = ToolCallRequest(
            tool_call={"name": "demo_tool", "args": {"x": 1}, "id": "call_1"},
            tool=None,
            state={"messages": []},
            runtime=Mock(),
        )

        async def handler(_req: ToolCallRequest):
            raise ValueError("boom")

        import pytest

        with pytest.raises(ValueError, match="boom"):
            await ToolCallLoggingMiddleware().awrap_tool_call(request, handler)

        messages = [r.getMessage() for r in caplog.records if r.name == "daiv.tools"]
        assert any("[demo_tool] Tool call (id=call_1" in m for m in messages)
