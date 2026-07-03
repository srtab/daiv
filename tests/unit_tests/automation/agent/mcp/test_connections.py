from __future__ import annotations

import types

from automation.agent.mcp.connections import build_connection, build_connections_and_filters
from automation.agent.mcp.schemas import ToolFilter, UserMcpServer


def test_build_connection_http():
    conn = build_connection("http", "http://u.test/mcp", {"H": "v"})
    assert conn["transport"] == "streamable_http"
    assert conn["url"] == "http://u.test/mcp"
    assert conn["headers"] == {"H": "v"}


def test_build_connection_sse():
    conn = build_connection("sse", "http://u.test/sse", None)
    assert conn["transport"] == "sse"
    assert conn["url"] == "http://u.test/sse"


def test_build_connection_unknown_transport_returns_none():
    assert build_connection("grpc", "http://u", None) is None


def test_servers_mapped_with_filters():
    servers = [
        ("a", UserMcpServer(type="http", url="http://a/mcp", tool_filter=ToolFilter(mode="allow", items=["x"]))),
        ("b", UserMcpServer(type="sse", url="http://b/sse")),
    ]
    connections, filters = build_connections_and_filters(servers)
    assert set(connections) == {"a", "b"}
    assert connections["a"]["transport"] == "streamable_http"
    assert connections["b"]["transport"] == "sse"
    assert filters["a"].items == ["x"]
    assert "b" not in filters


def test_unknown_transport_skipped_with_warning(caplog):
    """Never register a filter for a server that has no connection (it would vanish silently)."""
    dto = types.SimpleNamespace(
        type="grpc", url="http://u", headers=None, tool_filter=ToolFilter(mode="block", items=["x"])
    )
    with caplog.at_level("WARNING", logger="daiv.mcp"):
        connections, filters = build_connections_and_filters([("u", dto)])
    assert connections == {}
    assert filters == {}
    assert "unsupported transport" in caplog.text.lower()
