from unittest.mock import AsyncMock, MagicMock

from langchain_core.tools import StructuredTool

from automation.agent.middlewares.deferred_tools import DeferredToolsMiddleware


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


class TestDeferredToolsMiddleware:
    def test_exposes_tool_search_via_tools_attr(self):
        middleware = DeferredToolsMiddleware(always_loaded=set())
        assert len(middleware.tools) == 1
        assert middleware.tools[0].name == "tool_search"

    def test_state_schema_is_deferred_state(self):
        from automation.agent.deferred.state import DeferredToolsState

        middleware = DeferredToolsMiddleware(always_loaded=set())
        assert middleware.state_schema is DeferredToolsState

    async def test_appends_loaded_tools_to_request(self):
        github = _make_tool("github_create_issue", "Create issue")
        sentry = _make_tool("sentry_find_orgs", "List orgs")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github, sentry])

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

    async def test_always_loaded_tools_pass_through(self):
        read_file = _make_tool("read_file", "Read a file")
        github = _make_tool("github_create_issue", "Create issue")
        middleware = DeferredToolsMiddleware(always_loaded={"read_file"}, extra_tools=[github])

        request = _request(tools=[read_file], state={})
        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        names = [t.name for t in captured["tools"]]
        assert "read_file" in names
        assert "github_create_issue" not in names

    async def test_deferred_request_tools_filtered_out(self):
        read_file = _make_tool("read_file", "Read a file")
        web_fetch = _make_tool("web_fetch", "Fetch a URL")
        middleware = DeferredToolsMiddleware(always_loaded={"read_file"})

        request = _request(tools=[read_file, web_fetch], state={})
        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        names = [t.name for t in captured["tools"]]
        assert "read_file" in names
        assert "web_fetch" not in names

    async def test_request_tools_indexed_for_search(self):
        read_file = _make_tool("read_file", "Read a file")
        web_fetch = _make_tool("web_fetch", "Fetch a URL from the web")
        middleware = DeferredToolsMiddleware(always_loaded={"read_file"})

        request = _request(tools=[read_file, web_fetch], state={"loaded_tool_names": {"web_fetch"}})
        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        names = [t.name for t in captured["tools"]]
        assert "web_fetch" in names

    async def test_appends_block_to_system_prompt(self):
        github = _make_tool("github_create_issue", "Create issue")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

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
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

        request = _request(system_prompt="EXISTING PROMPT", state={"loaded_tool_names": {"github_create_issue"}})
        captured: dict = {}

        async def handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        assert "<available-deferred-tools>" not in captured["system_prompt"]

    async def test_system_prompt_none_does_not_pollute(self):
        github = _make_tool("github_create_issue", "Create issue")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

        request = _request(system_prompt=None, state={})
        captured: dict = {}

        async def handler(req):
            captured["system_prompt"] = req.system_prompt
            return MagicMock()

        await middleware.awrap_model_call(request, handler)

        assert not captured["system_prompt"].startswith("\n")
        assert "<available-deferred-tools>" in captured["system_prompt"]

    async def test_init_rejects_invalid_top_k(self):
        import pytest

        with pytest.raises(ValueError):
            DeferredToolsMiddleware(always_loaded=set(), top_k_default=10, top_k_max=5)
        with pytest.raises(ValueError):
            DeferredToolsMiddleware(always_loaded=set(), top_k_default=0, top_k_max=5)

    async def test_returns_handler_response(self):
        middleware = DeferredToolsMiddleware(always_loaded=set())
        sentinel = object()
        handler = AsyncMock(return_value=sentinel)

        result = await middleware.awrap_model_call(_request(), handler)
        assert result is sentinel

    def test_tool_search_is_always_loaded(self):
        # Even if the caller forgets, tool_search must be always-loaded — otherwise the
        # agent has no way to load deferred tools.
        middleware = DeferredToolsMiddleware(always_loaded=set())
        assert "tool_search" in middleware._always_loaded  # noqa: SLF001


class TestDeferredToolsMiddlewareCorrectiveMessages:
    async def test_corrective_message_for_unloaded_tool_call(self):
        from langchain_core.messages import AIMessage, ToolMessage

        github = _make_tool("github_create_issue", "Create issue")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

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
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

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

        middleware = DeferredToolsMiddleware(always_loaded={"read_file"})

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

    async def test_corrective_message_for_each_unloaded_tool_call(self):
        from langchain_core.messages import AIMessage, ToolMessage

        a = _make_tool("github_create_issue", "Create issue")
        b = _make_tool("sentry_find_orgs", "List orgs")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[a, b])

        ai_message = AIMessage(
            content="",
            tool_calls=[
                {"name": "github_create_issue", "id": "call_1", "args": {}, "type": "tool_call"},
                {"name": "sentry_find_orgs", "id": "call_2", "args": {}, "type": "tool_call"},
            ],
        )
        response = MagicMock()
        response.messages = [ai_message]

        async def handler(req):
            return response

        result = await middleware.awrap_model_call(_request(state={}), handler)

        tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
        assert sorted(m.tool_call_id for m in tool_messages) == ["call_1", "call_2"]

    async def test_response_without_messages_attr_does_not_crash(self):
        middleware = DeferredToolsMiddleware(always_loaded=set())

        class _Bare:
            pass

        async def handler(req):
            return _Bare()

        await middleware.awrap_model_call(_request(state={}), handler)

    async def test_stale_loaded_tool_name_dropped_with_warning(self, caplog):
        import logging

        github = _make_tool("github_create_issue", "Create issue")
        middleware = DeferredToolsMiddleware(always_loaded=set(), extra_tools=[github])

        captured: dict = {}

        async def handler(req):
            captured["tools"] = list(req.tools)
            return MagicMock()

        with caplog.at_level(logging.WARNING, logger="daiv.tools"):
            await middleware.awrap_model_call(_request(state={"loaded_tool_names": {"removed_tool"}}), handler)

        assert "removed_tool" in caplog.text
        assert "github_create_issue" not in [t.name for t in captured["tools"]]
