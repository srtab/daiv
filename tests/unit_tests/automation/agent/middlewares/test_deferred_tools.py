from unittest.mock import AsyncMock, MagicMock

from langchain_core.tools import StructuredTool

from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex
from automation.agent.middlewares.deferred_tools import DeferredMCPToolsMiddleware


def _make_tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **kwargs: "ok", name=name, description=description)


def _request(*, system_prompt: str = "", tools: list | None = None, state: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.system_prompt = system_prompt
    request.tools = list(tools or [])
    request.state = state or {}

    def _override(**kwargs) -> MagicMock:
        new = MagicMock()
        new.system_prompt = kwargs.get("system_prompt", request.system_prompt)
        new.tools = kwargs.get("tools", request.tools)
        new.state = request.state
        new.override = _override
        return new

    request.override = _override
    return request


class TestDeferredMCPToolsMiddleware:
    def test_exposes_tool_search_via_tools_attr(self):
        index = DeferredMCPToolsIndex([])
        middleware = DeferredMCPToolsMiddleware(index)
        assert len(middleware.tools) == 1
        assert middleware.tools[0].name == "tool_search"

    def test_state_schema_is_deferred_state(self):
        from automation.agent.mcp.deferred.state import DeferredMCPToolsState

        index = DeferredMCPToolsIndex([])
        middleware = DeferredMCPToolsMiddleware(index)
        assert middleware.state_schema is DeferredMCPToolsState

    async def test_appends_loaded_tools_to_request(self):
        github = _make_tool("github_create_issue", "Create issue")
        sentry = _make_tool("sentry_find_orgs", "List orgs")
        index = DeferredMCPToolsIndex([github, sentry])
        middleware = DeferredMCPToolsMiddleware(index)

        request = _request(state={"loaded_tool_names": {"github_create_issue"}})
        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        tool_names = [t.name for t in captured["tools"]]
        assert "github_create_issue" in tool_names
        assert "sentry_find_orgs" not in tool_names

    async def test_appends_always_loaded_tools(self):
        github = _make_tool("github_create_issue", "Create issue")
        index = DeferredMCPToolsIndex([github], always_loaded={"github_create_issue"})
        middleware = DeferredMCPToolsMiddleware(index)

        request = _request(state={})
        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        assert "github_create_issue" in [t.name for t in captured["tools"]]

    async def test_appends_block_to_system_prompt(self):
        github = _make_tool("github_create_issue", "Create issue")
        index = DeferredMCPToolsIndex([github])
        middleware = DeferredMCPToolsMiddleware(index)

        request = _request(system_prompt="EXISTING PROMPT", state={})
        captured: dict = {}

        async def handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        assert captured["system_prompt"].startswith("EXISTING PROMPT")
        assert "<available-deferred-tools>" in captured["system_prompt"]
        assert "github_create_issue" in captured["system_prompt"]

    async def test_no_block_when_all_tools_loaded(self):
        github = _make_tool("github_create_issue", "Create issue")
        index = DeferredMCPToolsIndex([github])
        middleware = DeferredMCPToolsMiddleware(index)

        request = _request(system_prompt="EXISTING PROMPT", state={"loaded_tool_names": {"github_create_issue"}})
        captured: dict = {}

        async def handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        # Block omitted when nothing remains deferred — system prompt stays clean.
        assert "<available-deferred-tools>" not in captured["system_prompt"]

    async def test_returns_handler_response(self):
        index = DeferredMCPToolsIndex([])
        middleware = DeferredMCPToolsMiddleware(index)
        sentinel = object()
        handler = AsyncMock(return_value=sentinel)

        result = await middleware.awrap_model_call(_request(), handler)
        assert result is sentinel


class TestDeferredMCPToolsMiddlewareHardFail:
    async def test_corrective_message_for_unloaded_tool_call(self):
        from langchain_core.messages import AIMessage, ToolMessage

        github = _make_tool("github_create_issue", "Create issue")
        index = DeferredMCPToolsIndex([github])
        middleware = DeferredMCPToolsMiddleware(index)

        ai_message = AIMessage(
            content="", tool_calls=[{"name": "github_create_issue", "id": "call_abc", "args": {}, "type": "tool_call"}]
        )
        response = MagicMock()
        response.messages = [ai_message]

        async def handler(req):
            return response

        request = _request(state={})
        result = await middleware.awrap_model_call(request, handler)

        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 1
        assert tool_messages[0].tool_call_id == "call_abc"
        assert "tool_search" in tool_messages[0].content
        assert "github_create_issue" in tool_messages[0].content

    async def test_no_corrective_message_for_loaded_tool_call(self):
        from langchain_core.messages import AIMessage, ToolMessage

        github = _make_tool("github_create_issue", "Create issue")
        index = DeferredMCPToolsIndex([github])
        middleware = DeferredMCPToolsMiddleware(index)

        ai_message = AIMessage(
            content="", tool_calls=[{"name": "github_create_issue", "id": "call_abc", "args": {}, "type": "tool_call"}]
        )
        response = MagicMock()
        response.messages = [ai_message]

        async def handler(req):
            return response

        request = _request(state={"loaded_tool_names": {"github_create_issue"}})
        result = await middleware.awrap_model_call(request, handler)

        assert not [m for m in result.messages if isinstance(m, ToolMessage)]

    async def test_no_corrective_message_for_non_deferred_tool(self):
        from langchain_core.messages import AIMessage, ToolMessage

        index = DeferredMCPToolsIndex([])
        middleware = DeferredMCPToolsMiddleware(index)

        ai_message = AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "id": "call_xyz", "args": {"path": "/x"}, "type": "tool_call"}],
        )
        response = MagicMock()
        response.messages = [ai_message]

        async def handler(req):
            return response

        request = _request(state={})
        result = await middleware.awrap_model_call(request, handler)

        assert not [m for m in result.messages if isinstance(m, ToolMessage)]
