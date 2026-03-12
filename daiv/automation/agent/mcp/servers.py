from .base import MCPServer
from .conf import settings
from .decorator import mcp_server
from .schemas import CommonOptions, StdioMcpServer, ToolFilter


@mcp_server
class SentryMCPServer(MCPServer):
    name = "sentry"
    proxy_config = StdioMcpServer(
        command="npx",
        args=[f"@sentry/mcp-server@{settings.SENTRY_VERSION}"],
        env={
            "SENTRY_HOST": settings.SENTRY_HOST or "",
            "SENTRY_ACCESS_TOKEN": settings.SENTRY_ACCESS_TOKEN
            and settings.SENTRY_ACCESS_TOKEN.get_secret_value()
            or "",
        },
        options=CommonOptions(
            panic_if_invalid=False,
            log_enabled=True,
            tool_filter=ToolFilter(mode="allow", items=["find_organizations", "get_issue_details"]),
        ),
    )

    def is_enabled(self) -> bool:
        return bool(settings.SENTRY_ENABLED and settings.SENTRY_ACCESS_TOKEN)


@mcp_server
class Context7MCPServer(MCPServer):
    name = "context7"
    proxy_config = StdioMcpServer(
        command="npx",
        args=[f"@upstash/context7-mcp@{settings.CONTEXT7_VERSION}"],
        env={"CONTEXT7_API_KEY": settings.CONTEXT7_API_KEY and settings.CONTEXT7_API_KEY.get_secret_value() or ""},
        options=CommonOptions(
            panic_if_invalid=False,
            log_enabled=True,
            tool_filter=ToolFilter(mode="allow", items=["resolve-library-id", "query-docs"]),
        ),
    )

    def is_enabled(self) -> bool:
        return bool(settings.CONTEXT7_ENABLED)
