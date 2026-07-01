from __future__ import annotations

from unittest.mock import MagicMock

from langchain_mcp_adapters.sessions import StreamableHttpConnection

from automation.agent.mcp.base import MCPServer
from automation.agent.mcp.registry import MCPRegistry
from automation.agent.mcp.schemas import ToolFilter, UserMcpServer


class _DummyBuiltin(MCPServer):
    name = "dummy"
    tool_filter = ToolFilter(mode="allow", items=["one"])

    def is_enabled(self) -> bool:
        return True

    def get_connection(self):  # type: ignore[override]
        conn = MagicMock(spec=StreamableHttpConnection)
        conn.url = "http://dummy"
        return conn


def test_builtin_only():
    reg = MCPRegistry()
    reg.register(_DummyBuiltin)
    connections, filters = reg.get_connections_and_filters()
    assert "dummy" in connections
    assert filters["dummy"].items == ["one"]


def test_user_http_server_added():
    reg = MCPRegistry()
    user = [("u", UserMcpServer(type="http", url="http://u.test/mcp", headers={"H": "v"}))]
    connections, _ = reg.get_connections_and_filters(user)
    assert "u" in connections
    assert connections["u"]["transport"] == "streamable_http"
    assert connections["u"]["url"] == "http://u.test/mcp"
    assert connections["u"]["headers"] == {"H": "v"}


def test_user_sse_server_added():
    reg = MCPRegistry()
    user = [("u", UserMcpServer(type="sse", url="http://u.test/sse"))]
    connections, _ = reg.get_connections_and_filters(user)
    assert "u" in connections
    assert connections["u"]["transport"] == "sse"
    assert connections["u"]["url"] == "http://u.test/sse"


def test_user_overrides_builtin_by_name():
    reg = MCPRegistry()
    reg.register(_DummyBuiltin)
    user = [("dummy", UserMcpServer(type="http", url="http://override"))]
    connections, _ = reg.get_connections_and_filters(user)
    # User connection wins
    assert connections["dummy"]["transport"] == "streamable_http"
    assert connections["dummy"]["url"] == "http://override"


def test_user_tool_filter_carried_through():
    reg = MCPRegistry()
    user = [("u", UserMcpServer(type="http", url="http://u", tool_filter=ToolFilter(mode="block", items=["x"])))]
    _, filters = reg.get_connections_and_filters(user)
    assert filters["u"].mode == "block"
    assert filters["u"].items == ["x"]


def test_user_unknown_transport_skipped(caplog):
    """An unrecognized transport must be skipped with a warning — never register a
    filter for a server that has no connection (which would otherwise vanish silently)."""
    import types

    reg = MCPRegistry()
    dto = types.SimpleNamespace(
        type="grpc", url="http://u", headers=None, tool_filter=ToolFilter(mode="block", items=["x"])
    )
    with caplog.at_level("WARNING", logger="daiv.mcp"):
        connections, filters = reg.get_connections_and_filters([("u", dto)])
    assert "u" not in connections
    assert "u" not in filters
    assert "unsupported transport" in caplog.text.lower()


def test_builtin_names():
    reg = MCPRegistry()
    reg.register(_DummyBuiltin)
    assert reg.builtin_names() == ["dummy"]
