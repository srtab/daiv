from daiv import USER_AGENT

from .base import MCPServer
from .conf import settings
from .decorator import mcp_server
from .schemas import CommonOptions, StdioMcpServer, ToolFilter


@mcp_server
class FetchMCPServer(MCPServer):
    name = "fetch"
    proxy_config = StdioMcpServer(
        command="uvx",
        args=[f"mcp-server-fetch=={settings.FETCH_VERSION}", "--user-agent", USER_AGENT],
        options=CommonOptions(panic_if_invalid=False, log_enabled=True),
    )

    def is_enabled(self) -> bool:
        return settings.FETCH_ENABLED


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
        return settings.SENTRY_ENABLED and settings.SENTRY_ACCESS_TOKEN is not None
