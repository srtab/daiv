from unittest.mock import AsyncMock, MagicMock, patch

from automation.agent.mcp.schemas import ToolFilter
from automation.agent.mcp.toolkits import MCPToolkit, _apply_tool_filters


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


class TestMCPToolkitGetTools:
    async def test_returns_empty_when_no_connections(self, monkeypatch):
        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda: [])
        monkeypatch.setattr(
            "automation.agent.mcp.registry.mcp_registry.get_connections_and_filters", lambda user_servers: ({}, {})
        )

        result = await MCPToolkit.get_tools()

        assert result == []

    async def test_passes_user_servers_to_registry(self, monkeypatch):
        captured = {}

        def fake_build():
            return [("my-server", MagicMock())]

        def fake_get_connections(user_servers):
            captured["user_servers"] = user_servers
            return ({}, {})

        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", fake_build)
        monkeypatch.setattr(
            "automation.agent.mcp.registry.mcp_registry.get_connections_and_filters", fake_get_connections
        )

        await MCPToolkit.get_tools()

        assert len(captured["user_servers"]) == 1
        assert captured["user_servers"][0][0] == "my-server"

    async def test_returns_tools_from_client(self, monkeypatch):
        mock_tool = MagicMock()
        mock_tool.name = "sentry_search_issues"
        mock_tool.tags = []
        mock_tool.metadata = {}

        fake_connection = {"type": "streamable_http", "url": "http://example.com/mcp"}
        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda: [])
        monkeypatch.setattr(
            "automation.agent.mcp.registry.mcp_registry.get_connections_and_filters",
            lambda user_servers: ({"sentry": fake_connection}, {}),
        )

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(return_value=[mock_tool])

        with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=mock_client):
            result = await MCPToolkit.get_tools()

        assert len(result) == 1
        assert result[0].name == "sentry_search_issues"
        assert result[0].handle_tool_error is True
        assert result[0].handle_validation_error is True
        assert "mcp_server" in result[0].tags

    async def test_returns_empty_on_client_error(self, monkeypatch):
        fake_connection = {"type": "streamable_http", "url": "http://example.com/mcp"}
        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda: [])
        monkeypatch.setattr(
            "automation.agent.mcp.registry.mcp_registry.get_connections_and_filters",
            lambda user_servers: ({"sentry": fake_connection}, {}),
        )

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=Exception("connection refused"))

        with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=mock_client):
            result = await MCPToolkit.get_tools()

        assert result == []
