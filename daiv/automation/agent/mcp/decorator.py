from typing import TYPE_CHECKING

from .registry import mcp_registry

if TYPE_CHECKING:
    from .base import MCPServer


def mcp_server(cls: type[MCPServer]) -> type[MCPServer]:
    """
    Decorator to register a MCP server.
    """
    mcp_registry.register(cls)
    return cls
