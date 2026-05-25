from __future__ import annotations

import logging
from inspect import isclass
from typing import TYPE_CHECKING

from langchain_mcp_adapters.sessions import SSEConnection, StreamableHttpConnection

from .base import MCPServer

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection

    from .schemas import ToolFilter, UserMcpServer


logger = logging.getLogger("daiv.mcp")


class MCPRegistry:
    """In-process registry of code-defined (built-in) MCP server classes.

    User-defined MCP servers are loaded from the DB by
    ``mcp_servers.services.build_runtime_servers()`` and passed into
    :meth:`get_connections_and_filters` by the caller (the toolkit).
    """

    def __init__(self):
        self._registry: list[type[MCPServer]] = []

    def register(self, server: type[MCPServer]) -> None:
        assert isclass(server) and issubclass(server, MCPServer), (
            f"{server} must be a class that inherits from MCPServer"
        )
        assert server not in self._registry, f"{server} is already registered as MCP server."
        self._registry.append(server)

    def builtin_names(self) -> list[str]:
        """Return the names of registered built-in server classes (used by
        the upsert on app ready)."""
        return [cls.name for cls in self._registry]

    def get_connections_and_filters(
        self, user_servers: list[tuple[str, UserMcpServer]] | None = None
    ) -> tuple[dict[str, Connection], dict[str, ToolFilter]]:
        """Build the connection + tool-filter maps that the toolkit feeds to
        ``MultiServerMCPClient``.

        Built-in (code-defined) servers come from the registered classes.
        ``user_servers`` is a list of ``(name, UserMcpServer)`` tuples
        produced by the DB adapter. A collision in name means the user/DB
        entry wins (matches prior behavior).
        """
        connections: dict[str, Connection] = {}
        filters: dict[str, ToolFilter] = {}

        for server_class in self._registry:
            server = server_class()
            if server.is_enabled():
                connections[server.name] = server.get_connection()
                if server.tool_filter is not None:
                    filters[server.name] = server.tool_filter

        for name, dto in user_servers or []:
            headers = dto.headers or None
            if dto.type == "sse":
                connections[name] = SSEConnection(transport="sse", url=dto.url, headers=headers)
            elif dto.type == "http":
                connections[name] = StreamableHttpConnection(transport="streamable_http", url=dto.url, headers=headers)
            if dto.tool_filter is not None:
                filters[name] = dto.tool_filter

        return connections, filters


mcp_registry = MCPRegistry()
