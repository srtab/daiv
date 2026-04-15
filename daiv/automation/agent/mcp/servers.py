from langchain_mcp_adapters.sessions import StreamableHttpConnection

from .base import MCPServer
from .conf import settings
from .decorator import mcp_server
from .schemas import ToolFilter


@mcp_server
class SentryMCPServer(MCPServer):
    name = "sentry"
    tool_filter = ToolFilter(mode="allow", items=["find_organizations", "find_projects", "list_issues", "list_events"])

    def is_enabled(self) -> bool:
        return settings.SENTRY_URL is not None

    def get_connection(self) -> StreamableHttpConnection:
        assert settings.SENTRY_URL is not None  # guaranteed by is_enabled()
        return StreamableHttpConnection(transport="streamable_http", url=settings.SENTRY_URL)


@mcp_server
class Context7MCPServer(MCPServer):
    name = "context7"
    tool_filter = ToolFilter(mode="allow", items=["resolve-library-id", "query-docs"])

    def is_enabled(self) -> bool:
        return settings.CONTEXT7_URL is not None

    def get_connection(self) -> StreamableHttpConnection:
        assert settings.CONTEXT7_URL is not None  # guaranteed by is_enabled()
        return StreamableHttpConnection(transport="streamable_http", url=settings.CONTEXT7_URL)
