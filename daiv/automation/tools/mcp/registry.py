from __future__ import annotations

from inspect import isclass
from typing import TYPE_CHECKING

from .base import MCPServer

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection


class MCPRegistry:
    """
    Registry that keeps track of the registered MCP servers.
    """

    def __init__(self):
        self._registry: list[type[MCPServer]] = []

    def register(self, server: type[MCPServer]) -> None:
        assert isclass(server) and issubclass(server, MCPServer), (
            f"{server} must be a class that inherits from MCPServer"
        )
        assert server not in self._registry, f"{server} is already registered as MCP server."

        self._registry.append(server)

    def get_connections(self) -> dict[str, Connection]:
        servers: dict[str, Connection] = {}

        for server_class in self._registry:
            server = server_class()
            if server.is_enabled():
                servers[server.name] = server.connection

        return servers


mcp_registry = MCPRegistry()
