from langchain_mcp_adapters.sessions import SSEConnection

from .base import MCPServer
from .conf import settings
from .decorator import mcp_server


@mcp_server
class FetchMCPServer(MCPServer):
    name = "fetch"
    connection = SSEConnection(transport="sse", url="http://mcp-proxy:9090/fetch/sse")

    def is_enabled(self) -> bool:
        return settings.FETCH_ENABLED


@mcp_server
class SentryMCPServer(MCPServer):
    name = "sentry"
    connection = SSEConnection(transport="sse", url="http://mcp-proxy:9090/sentry/sse")

    def is_enabled(self) -> bool:
        return settings.SENTRY_ENABLED
