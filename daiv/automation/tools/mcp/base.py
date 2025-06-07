from abc import ABC

from langchain_mcp_adapters.sessions import SSEConnection

from core.utils import build_uri

from .conf import settings
from .schemas import SseMcpServer, StdioMcpServer, StreamableHttpMcpServer


class MCPServer(ABC):
    """
    Base class for MCP servers.
    """

    name: str
    proxy_config: StdioMcpServer | SseMcpServer | StreamableHttpMcpServer

    def is_enabled(self) -> bool:
        """
        Check if the MCP server is enabled.

        Returns:
            bool: True if the MCP server is enabled, False otherwise.
        """
        return True

    def get_connection(self) -> SSEConnection:
        """
        Get the mcp adapter connection to the MCP proxy. It only supports SSE connections.

        Returns:
            SSEConnection: The SSE connection to the MCP proxy.
        """
        return SSEConnection(
            transport="sse", url=build_uri(settings.MCP_PROXY_HOST.encoded_string(), f"{self.name}/sse")
        )

    def get_proxy_config(self) -> StdioMcpServer | SseMcpServer | StreamableHttpMcpServer:
        """
        Get the connection details to launch the MCP servers in the MCP proxy.

        Returns:
            StdioMcpServer | SseMcpServer | StreamableHttpMcpServer: The connection to feed the MCP proxy.
        """
        return self.proxy_config
