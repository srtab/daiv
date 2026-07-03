import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
            "automation.agent.mcp.toolkits.build_connections_and_filters", lambda user_servers: ({}, {})
        )

        result = await MCPToolkit.get_tools()

        assert result == []

    async def test_one_failing_server_does_not_blank_the_others(self, monkeypatch):
        """A single broken endpoint must not remove tools from healthy peers."""
        good_tool = MagicMock()
        good_tool.name = "good_t"
        good_tool.tags = []
        good_tool.metadata = {}

        bad_conn = {"transport": "streamable_http", "url": "http://bad/mcp"}
        good_conn = {"transport": "streamable_http", "url": "http://good/mcp"}
        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda: [])
        monkeypatch.setattr(
            "automation.agent.mcp.toolkits.build_connections_and_filters",
            lambda user_servers: ({"bad": bad_conn, "good": good_conn}, {}),
        )

        def _client_factory(connections, **kwargs):
            client = MagicMock()
            name = next(iter(connections))
            if name == "bad":
                client.get_tools = AsyncMock(side_effect=RuntimeError("dns fail"))
            else:
                client.get_tools = AsyncMock(return_value=[good_tool])
            return client

        with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", side_effect=_client_factory):
            result = await MCPToolkit.get_tools()

        assert [t.name for t in result] == ["good_t"]

    async def test_hanging_server_times_out_without_blanking_peers(self, monkeypatch):
        """A server that hangs must time out (not freeze) and not blank tools from healthy peers."""
        good_tool = MagicMock()
        good_tool.name = "good_t"
        good_tool.tags = []
        good_tool.metadata = {}

        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda: [])
        monkeypatch.setattr(
            "automation.agent.mcp.toolkits.build_connections_and_filters",
            lambda user_servers: ({"slow": {"url": "http://slow/mcp"}, "good": {"url": "http://good/mcp"}}, {}),
        )

        async def _hang():
            await asyncio.sleep(10)  # never returns within the timeout
            return [good_tool]

        def _client_factory(connections, **kwargs):
            client = MagicMock()
            if next(iter(connections)) == "slow":
                client.get_tools = _hang
            else:
                client.get_tools = AsyncMock(return_value=[good_tool])
            return client

        with (
            patch("automation.agent.mcp.toolkits.MultiServerMCPClient", side_effect=_client_factory),
            patch("automation.agent.mcp.toolkits.settings") as mcp_settings,
        ):
            mcp_settings.TOOL_LOAD_TIMEOUT = 0.05
            result = await MCPToolkit.get_tools()

        assert [t.name for t in result] == ["good_t"]

    async def test_passes_servers_to_connection_builder(self, monkeypatch):
        captured = {}

        def fake_build():
            return [("my-server", MagicMock())]

        def fake_get_connections(user_servers):
            captured["user_servers"] = user_servers
            return ({}, {})

        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", fake_build)
        monkeypatch.setattr("automation.agent.mcp.toolkits.build_connections_and_filters", fake_get_connections)

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
            "automation.agent.mcp.toolkits.build_connections_and_filters",
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
            "automation.agent.mcp.toolkits.build_connections_and_filters",
            lambda user_servers: ({"sentry": fake_connection}, {}),
        )

        mock_client = MagicMock()
        mock_client.get_tools = AsyncMock(side_effect=Exception("connection refused"))

        with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", return_value=mock_client):
            result = await MCPToolkit.get_tools()

        assert result == []


@pytest.mark.django_db(transaction=True)
async def test_end_to_end_db_row_yields_tool():
    """Exercise the full DB → build_runtime_servers → registry → client chain so a rename of any
    boundary symbol breaks this test instead of slipping through the mocked happy path."""
    from asgiref.sync import sync_to_async
    from mcp_servers.models import MCPServer

    @sync_to_async
    def _create():
        MCPServer.objects.create(
            name="acme", transport=MCPServer.Transport.HTTP, url="http://acme.test/mcp", enabled=True
        )

    await _create()

    def _client_factory(connections, **kwargs):
        name = next(iter(connections))
        client = MagicMock()
        if name == "acme":
            tool = MagicMock()
            tool.name = "acme_search"
            tool.tags = []
            tool.metadata = {}
            client.get_tools = AsyncMock(return_value=[tool])
        else:
            client.get_tools = AsyncMock(return_value=[])
        return client

    with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", side_effect=_client_factory):
        result = await MCPToolkit.get_tools()

    assert "acme_search" in [t.name for t in result]


@pytest.mark.django_db(transaction=True)
async def test_end_to_end_db_tool_filter_applied():
    """A persisted block-mode filter must be honored through the real
    build_runtime_servers → registry → get_tools → _apply_tool_filters chain
    (every other get_tools test passes an empty filter dict)."""
    from asgiref.sync import sync_to_async
    from mcp_servers.models import MCPServer

    @sync_to_async
    def _create():
        MCPServer.objects.create(
            name="acme",
            transport=MCPServer.Transport.HTTP,
            url="http://acme.test/mcp",
            enabled=True,
            tool_filter_mode=MCPServer.FilterMode.BLOCK,
            tool_filter_items=["secret"],
        )

    await _create()

    def _client_factory(connections, **kwargs):
        # Tool names are server-prefixed (tool_name_prefix=True); the filter strips "acme_".
        names = ["acme_search", "acme_secret"]
        tools = []
        for n in names:
            t = MagicMock()
            t.name = n
            t.tags = []
            t.metadata = {}
            tools.append(t)
        client = MagicMock()
        client.get_tools = AsyncMock(return_value=tools)
        return client

    with patch("automation.agent.mcp.toolkits.MultiServerMCPClient", side_effect=_client_factory):
        result = await MCPToolkit.get_tools()

    names = [t.name for t in result]
    assert "acme_search" in names
    assert "acme_secret" not in names  # blocked by the persisted filter
