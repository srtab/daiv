from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .schemas import ToolFilter  # noqa: TC001

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection


class MCPServer(ABC):
    """
    Base class for MCP servers.
    """

    name: str
    tool_filter: ToolFilter | None = None

    def is_enabled(self) -> bool:
        """
        Check if the MCP server is enabled.

        Returns:
            bool: True if the MCP server is enabled, False otherwise.
        """
        return True

    @abstractmethod
    def get_connection(self) -> Connection:
        """
        Get the connection configuration for this MCP server.

        Returns:
            Connection: The connection configuration for the langchain MCP adapter.
        """
        ...
