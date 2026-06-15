import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from automation.agent.mcp.schemas import ToolFilter
from automation.agent.mcp.toolkits import MCPToolkit, _apply_tool_filters, _load_server_tools


def _make_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    return tool


class TestApplyToolFilters:
    def test_allow_mode_passes_matching_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "sentry_search_issues"

    def test_allow_mode_blocks_non_matching_tools(self):
        tools = [_make_tool("sentry_delete_project")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 0

    def test_block_mode_removes_matching_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="block", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "sentry_find_projects"

    def test_block_mode_passes_non_matching_tools(self):
        tools = [_make_tool("sentry_find_projects")]
        filters = {"sentry": ToolFilter(mode="block", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1

    def test_unmatched_tools_pass_through(self):
        tools = [_make_tool("custom_tool")]
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters(tools, filters)

        assert len(result) == 1
        assert result[0].name == "custom_tool"

    def test_empty_tools_returns_empty(self):
        filters = {"sentry": ToolFilter(mode="allow", items=["search_issues"])}

        result = _apply_tool_filters([], filters)

        assert result == []

    def test_empty_filters_returns_all_tools(self):
        tools = [_make_tool("sentry_search_issues"), _make_tool("context7_query-docs")]

        result = _apply_tool_filters(tools, {})

        assert len(result) == 2

    def test_multiple_servers_filtered_independently(self):
        tools = [
            _make_tool("sentry_search_issues"),
            _make_tool("sentry_delete_project"),
            _make_tool("context7_query-docs"),
            _make_tool("context7_resolve-library-id"),
        ]
        filters = {
            "sentry": ToolFilter(mode="allow", items=["search_issues"]),
            "context7": ToolFilter(mode="block", items=["resolve-library-id"]),
        }

        result = _apply_tool_filters(tools, filters)

        names = [t.name for t in result]
        assert "sentry_search_issues" in names
        assert "sentry_delete_project" not in names
        assert "context7_query-docs" in names
        assert "context7_resolve-library-id" not in names


class TestLoadServerTools:
    async def test_returns_tools_on_success(self):
        client = MagicMock()
        client.get_tools = AsyncMock(return_value=[_make_tool("sentry_whoami")])

        result = await _load_server_tools(client, "sentry", timeout=5.0)

        assert [t.name for t in result] == ["sentry_whoami"]

    async def test_returns_empty_when_server_times_out(self):
        async def _hang(*, server_name):
            await asyncio.sleep(10)
            return [_make_tool("sentry_whoami")]

        client = MagicMock()
        client.get_tools = _hang

        result = await _load_server_tools(client, "sentry", timeout=0.01)

        assert result == []

    async def test_returns_empty_when_server_raises(self):
        client = MagicMock()
        client.get_tools = AsyncMock(side_effect=RuntimeError("boom"))

        result = await _load_server_tools(client, "sentry", timeout=5.0)

        assert result == []


class TestGetToolsResilience:
    async def test_failing_server_does_not_drop_healthy_server_tools(self):
        async def _fake_get_tools(*, server_name=None):
            if server_name == "good":
                return [_make_tool("good_tool")]
            raise RuntimeError("bad server is down")

        fake_client = MagicMock()
        fake_client.get_tools = _fake_get_tools

        with (
            patch("automation.agent.mcp.registry.mcp_registry") as registry,
            patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=fake_client),
        ):
            registry.get_connections_and_filters.return_value = ({"good": MagicMock(), "bad": MagicMock()}, {})

            tools = await MCPToolkit.get_tools()

        assert [t.name for t in tools] == ["good_tool"]

    async def test_hanging_server_does_not_block_healthy_server_tools(self):
        async def _fake_get_tools(*, server_name=None):
            if server_name == "good":
                return [_make_tool("good_tool")]
            await asyncio.sleep(10)  # simulate a server that hangs and never returns
            return [_make_tool("bad_tool")]

        fake_client = MagicMock()
        fake_client.get_tools = _fake_get_tools

        with (
            patch("automation.agent.mcp.registry.mcp_registry") as registry,
            patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=fake_client),
            patch("automation.agent.mcp.toolkits.settings") as mcp_settings,
        ):
            mcp_settings.TOOL_LOAD_TIMEOUT = 0.05
            registry.get_connections_and_filters.return_value = ({"good": MagicMock(), "bad": MagicMock()}, {})

            tools = await MCPToolkit.get_tools()

        assert [t.name for t in tools] == ["good_tool"]

    async def test_merges_tools_from_multiple_healthy_servers(self):
        async def _fake_get_tools(*, server_name=None):
            return [_make_tool(f"{server_name}_tool")]

        fake_client = MagicMock()
        fake_client.get_tools = _fake_get_tools

        with (
            patch("automation.agent.mcp.registry.mcp_registry") as registry,
            patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=fake_client),
        ):
            registry.get_connections_and_filters.return_value = ({"sentry": MagicMock(), "context7": MagicMock()}, {})

            tools = await MCPToolkit.get_tools()

        assert {t.name for t in tools} == {"sentry_tool", "context7_tool"}
        # Every returned tool is decorated so the agent keeps running when an MCP tool errors.
        assert all(t.handle_tool_error is True and t.tags == ["mcp_server"] for t in tools)

    async def test_returns_empty_when_no_servers_configured(self):
        with patch("automation.agent.mcp.registry.mcp_registry") as registry:
            registry.get_connections_and_filters.return_value = ({}, {})

            tools = await MCPToolkit.get_tools()

        assert tools == []

    async def test_applies_tool_filters_to_loaded_tools(self):
        async def _fake_get_tools(*, server_name=None):
            return [_make_tool("sentry_whoami"), _make_tool("sentry_delete_project")]

        fake_client = MagicMock()
        fake_client.get_tools = _fake_get_tools

        with (
            patch("automation.agent.mcp.registry.mcp_registry") as registry,
            patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=fake_client),
        ):
            registry.get_connections_and_filters.return_value = (
                {"sentry": MagicMock()},
                {"sentry": ToolFilter(mode="allow", items=["whoami"])},
            )

            tools = await MCPToolkit.get_tools()

        assert [t.name for t in tools] == ["sentry_whoami"]
