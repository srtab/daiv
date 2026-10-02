from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from automation.agent.toolkits import BaseToolkit

from .client import build_client, list_tools
from .conf import settings
from .errors import FailureKind, classify

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import UserMcpServer

logger = logging.getLogger("daiv.tools")

_SOFT_FAILURES = frozenset({FailureKind.TIMEOUT, FailureKind.SERVER_ERROR, FailureKind.STREAM_BROKEN})


async def _load_server_tools(name: str, server: UserMcpServer) -> list[BaseTool]:
    """Load, filter and prefix one server's tools. Bounded by ``TOOL_LOAD_TIMEOUT``; no ``Exception`` escapes:
    a hang or any error degrades to an empty list so one endpoint can neither freeze nor blank its peers.
    """
    try:
        tools = await asyncio.wait_for(
            list_tools(build_client(server.type, server.url, server.headers)), timeout=settings.TOOL_LOAD_TIMEOUT
        )
    except Exception as exc:  # never BaseException: CancelledError must propagate
        failure = classify(exc)
        if failure.kinds <= _SOFT_FAILURES:
            detail = (
                f"timed out after {settings.TOOL_LOAD_TIMEOUT:g}s"
                if failure.kinds == {FailureKind.TIMEOUT}
                else failure.message
            )
            logger.warning("Failed to load tools from MCP server %r (%s): %s; skipping it", name, server.url, detail)
        else:
            logger.exception("Error getting tools from MCP server %r (%s); skipping it", name, server.url)
        return []

    if server.tool_filter is not None:
        tools = [tool for tool in tools if server.tool_filter.allows(tool.name)]
    for tool in tools:
        tool.name = f"{name}_{tool.name}"
        tool.tags = ["mcp_server"]
        tool.metadata = {**(tool.metadata or {}), "mcp_server": name}
    return tools


class MCPToolkit(BaseToolkit):
    @classmethod
    async def get_tools(cls, user_id: int | None = None, overrides: dict | None = None) -> list[BaseTool]:
        from asgiref.sync import sync_to_async
        from mcp_connectors.services import build_runtime_servers

        servers = await sync_to_async(build_runtime_servers)(user_id, overrides)
        if not servers:
            return []

        logger.debug("Connecting to MCP servers: %s", {name: server.url for name, server in servers})
        per_server = await asyncio.gather(*(_load_server_tools(name, server) for name, server in servers))
        return [tool for server_tools in per_server for tool in server_tools]
