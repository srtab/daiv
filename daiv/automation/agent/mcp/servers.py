from langchain_mcp_adapters.sessions import SSEConnection

from .base import MCPServer
from .conf import settings
from .decorator import mcp_server
from .schemas import ToolFilter


@mcp_server
class SentryMCPServer(MCPServer):
    name = "sentry"
    tool_filter = ToolFilter(
        mode="allow",
        items=["find_organizations", "find_projects", "search_issues", "search_events", "get_issue_details"],
    )

    def is_enabled(self) -> bool:
        return settings.SENTRY_URL is not None

    def get_connection(self) -> SSEConnection:
        assert settings.SENTRY_URL is not None  # guaranteed by is_enabled()
        return SSEConnection(transport="sse", url=settings.SENTRY_URL)


@mcp_server
class Context7MCPServer(MCPServer):
    name = "context7"
    tool_filter = ToolFilter(mode="allow", items=["resolve-library-id", "query-docs"])

    def is_enabled(self) -> bool:
        return settings.CONTEXT7_URL is not None

    def get_connection(self) -> SSEConnection:
        assert settings.CONTEXT7_URL is not None  # guaranteed by is_enabled()
        return SSEConnection(transport="sse", url=settings.CONTEXT7_URL)
