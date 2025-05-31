# decorator to register a MCP server
from functools import wraps

from .base import MCPServer


def mcp_server(cls: type[MCPServer]) -> type[MCPServer]:
    """
    Decorator to register a MCP server.

    Returns:
        The MCP server class.
    """
    from .registry import mcp_registry

    mcp_registry.register(cls)

    @wraps(cls)
    def wrapper() -> type[MCPServer]:
        return cls

    return wrapper
