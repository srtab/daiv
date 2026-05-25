from langchain_mcp_adapters.sessions import StreamableHttpConnection

from .base import MCPServer
from .conf import settings
from .decorator import mcp_server
from .schemas import ToolFilter


@mcp_server
class SentryMCPServer(MCPServer):
    """
    Read-only tools allowed.
    """

    name = "sentry"
    tool_filter = ToolFilter(
        mode="allow",
        items=[
            "whoami",
            "find_teams",
            "get_issue_tag_values",
            "get_event_attachment",
            "get_replay_details",
            "get_profile_details",
            "get_sentry_resource",
            "find_organizations",
            "find_projects",
            "find_releases",
            "search_events",
            "search_issue",
            "search_issue_events",
        ],
    )

    def is_enabled(self) -> bool:
        return settings.SENTRY_URL is not None and super().is_enabled()

    def get_connection(self) -> StreamableHttpConnection:
        assert settings.SENTRY_URL is not None  # guaranteed by is_enabled()
        return StreamableHttpConnection(transport="streamable_http", url=settings.SENTRY_URL)


@mcp_server
class Context7MCPServer(MCPServer):
    name = "context7"
    tool_filter = ToolFilter(mode="allow", items=["resolve-library-id", "query-docs"])

    def is_enabled(self) -> bool:
        return settings.CONTEXT7_URL is not None and super().is_enabled()

    def get_connection(self) -> StreamableHttpConnection:
        assert settings.CONTEXT7_URL is not None  # guaranteed by is_enabled()
        return StreamableHttpConnection(transport="streamable_http", url=settings.CONTEXT7_URL)
