# decorator to register a MCP server
from functools import wraps
from typing import TYPE_CHECKING

from .registry import mcp_registry

if TYPE_CHECKING:
    from .base import MCPServer


def mcp_server(cls: type[MCPServer]) -> type[MCPServer]:
    """
    Decorator to register a MCP server.

    Returns:
        The MCP server class.
    """

    mcp_registry.register(cls)

    @wraps(cls)
    def wrapper(*args, **kwargs) -> MCPServer:
        return cls(*args, **kwargs)

    return wrapper
