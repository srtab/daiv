from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_mcp_adapters.sessions import SSEConnection, StreamableHttpConnection

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection

    from .schemas import ToolFilter, UserMcpServer

logger = logging.getLogger("daiv.mcp")


def build_connection(transport: str, url: str, headers: dict[str, str] | None) -> Connection | None:
    """Map primitive connection params to a langchain ``Connection``.

    Returns ``None`` for unsupported transports so callers decide whether to
    skip (runtime) or raise (test-connection probe).
    """
    if transport == "sse":
        return SSEConnection(transport="sse", url=url, headers=headers)
    if transport == "http":
        return StreamableHttpConnection(transport="streamable_http", url=url, headers=headers)
    return None


def build_connections_and_filters(
    servers: list[tuple[str, UserMcpServer]],
) -> tuple[dict[str, Connection], dict[str, ToolFilter]]:
    """Build the connection + tool-filter maps the toolkit feeds to ``MultiServerMCPClient``.

    ``servers`` is the ``(name, dto)`` list produced by
    ``mcp_servers.services.build_runtime_servers`` — built-in and custom rows
    alike; names are unique at the DB layer so there is no merge logic.
    """
    connections: dict[str, Connection] = {}
    filters: dict[str, ToolFilter] = {}
    for name, dto in servers:
        connection = build_connection(dto.type, dto.url, dto.headers or None)
        if connection is None:
            # Never register a filter for a server with no connection (which would
            # otherwise vanish without a trace).
            logger.warning("MCP server %r has unsupported transport %r; skipping it", name, dto.type)
            continue
        connections[name] = connection
        if dto.tool_filter is not None:
            filters[name] = dto.tool_filter
    return connections, filters
