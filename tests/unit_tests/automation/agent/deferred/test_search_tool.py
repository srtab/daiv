from unittest.mock import Mock

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command

from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.search_tool import make_tool_search


def _make_tool(name: str, description: str) -> StructuredTool:
    return StructuredTool.from_function(func=lambda **kwargs: "ok", name=name, description=description)


def _runtime(state: dict | None = None) -> ToolRuntime:
    return ToolRuntime(
        state=state or {"loaded_tool_names": set()},
        context=Mock(),
        config={},
        stream_writer=Mock(),
        tool_call_id="call_123",
        store=None,
    )


class TestToolSearch:
    async def test_search_loads_top_k(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue"),
            _make_tool("sentry_find_orgs", "List Sentry organizations"),
        ]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "github issue", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert "github_create_issue" in result.update["loaded_tool_names"]
        msg = result.update["messages"][0]
        assert isinstance(msg, ToolMessage)
        assert msg.tool_call_id == "call_123"
        assert "Loaded" in msg.content
        assert "github_create_issue" in msg.content

    async def test_search_with_no_results_returns_message_only(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "totally_unrelated_xyzzy", "runtime": _runtime()})

        assert isinstance(result, Command)
        assert "loaded_tool_names" not in result.update
        msg = result.update["messages"][0]
        assert "totally_unrelated_xyzzy" in msg.content

    async def test_select_loads_exact_names(self):
        tools = [
            _make_tool("github_create_issue", "Create a GitHub issue"),
            _make_tool("sentry_find_orgs", "List Sentry organizations"),
        ]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["sentry_find_orgs"], "runtime": _runtime()})

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"sentry_find_orgs"}

    async def test_select_unknown_name_surfaces_in_message(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({
            "query": "",
            "select": ["does_not_exist", "github_create_issue"],
            "runtime": _runtime(),
        })

        assert isinstance(result, Command)
        assert result.update["loaded_tool_names"] == {"github_create_issue"}
        assert "does_not_exist" in result.update["messages"][0].content

    async def test_select_all_unknown_returns_dedicated_message(self):
        index = DeferredToolsIndex([_make_tool("github_create_issue", "Create issue")])
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        result = await tool_search.ainvoke({"query": "", "select": ["nope_a", "nope_b"], "runtime": _runtime()})

        assert "loaded_tool_names" not in result.update
        msg_content = result.update["messages"][0].content
        assert "nope_a" in msg_content and "nope_b" in msg_content

    async def test_reads_existing_loaded_state(self):
        tools = [_make_tool("github_create_issue", "Create issue"), _make_tool("sentry_find_orgs", "List orgs")]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=10)

        runtime = _runtime({"loaded_tool_names": {"sentry_find_orgs"}})
        result = await tool_search.ainvoke({"query": "", "select": ["github_create_issue"], "runtime": runtime})

        assert result.update["loaded_tool_names"] == {"sentry_find_orgs", "github_create_issue"}

    async def test_top_k_clamped_to_max(self):
        tools = [_make_tool(f"helper_tool_{i}", f"helper number {i}") for i in range(20)]
        index = DeferredToolsIndex(tools)
        tool_search = make_tool_search(lambda: index, top_k_default=5, top_k_max=3)

        result = await tool_search.ainvoke({"query": "helper", "top_k": 50, "runtime": _runtime()})

        assert isinstance(result, Command)
        assert len(result.update["loaded_tool_names"]) <= 3
