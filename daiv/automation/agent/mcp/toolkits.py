from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

from automation.agent.toolkits import BaseToolkit

from .conf import settings
from .connections import build_connections_and_filters

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import ToolFilter

logger = logging.getLogger("daiv.tools")


def _get_connection_url(conn) -> str:
    return getattr(conn, "url", "unknown")


async def _load_server_tools(server_name: str, connection, timeout: float) -> list[BaseTool]:
    """
    Load tools from a single MCP server through its own client. Bounded by ``timeout`` and never raises:
    a hang (e.g. a broken handshake) or any error degrades to an empty tool list. Callers fan this out
    per server so one slow/broken endpoint can neither freeze nor blank tools from healthy peers.
    """
    client = MultiServerMCPClient({server_name: connection}, tool_name_prefix=True)
    try:
        return await asyncio.wait_for(client.get_tools(), timeout=timeout)
    except TimeoutError:
        # Anticipated degradation (server didn't answer within the timeout) — warning, no traceback.
        logger.warning("Timed out loading tools from MCP server %r after %ss; skipping it", server_name, timeout)
        return []
    except Exception:
        # Catch Exception, never BaseException: CancelledError (a BaseException) must propagate so outer
        # cancellation/shutdown isn't swallowed. logger.exception logs at error level with the traceback,
        # setting unexpected failures apart from the routine timeouts above.
        logger.exception(
            "Error getting tools from MCP server %r (%s); skipping it", server_name, _get_connection_url(connection)
        )
        return []


class MCPToolkit(BaseToolkit):
    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        from asgiref.sync import sync_to_async
        from mcp_servers.services import build_runtime_servers

        servers = await sync_to_async(build_runtime_servers)()
        connections, tool_filters = build_connections_and_filters(servers)

        if not connections:
            return []

        server_urls = {name: _get_connection_url(conn) for name, conn in connections.items()}
        logger.debug("Connecting to MCP servers: %s", server_urls)

        # Load each server independently (own client, bounded by a timeout) so one slow/broken/hanging
        # endpoint can neither freeze nor blank tools from healthy peers.
        per_server = await asyncio.gather(
            *(_load_server_tools(name, conn, settings.TOOL_LOAD_TIMEOUT) for name, conn in connections.items())
        )
        tools = [tool for server_tools in per_server for tool in server_tools]

        if tool_filters:
            tools = _apply_tool_filters(tools, tool_filters)

        for tool in tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True
            tool.tags = ["mcp_server"]
            tool.metadata = {"mcp_server": tool.name}

        return tools


def _apply_tool_filters(tools: list[BaseTool], filters: dict[str, ToolFilter]) -> list[BaseTool]:
    """Apply per-server allow/block filters.

    MCP tool names are server-prefixed (e.g. ``sentry_find_organizations``)
    because ``_load_server_tools`` builds each client with
    ``tool_name_prefix=True``. This strips the ``"{server_name}_"`` prefix to
    match a tool against its server's ``tool_filter.items`` — so this filter
    silently depends on that flag staying set. A tool whose name matches no
    configured filter prefix is passed through unchanged.
    """
    filtered = []
    for tool in tools:
        matched = False
        for server_name, tool_filter in filters.items():
            prefix = f"{server_name}_"
            if not tool.name.startswith(prefix):
                continue
            matched = True
            base_name = tool.name[len(prefix) :]
            if (tool_filter.mode == "allow" and base_name in tool_filter.items) or (
                tool_filter.mode == "block" and base_name not in tool_filter.items
            ):
                filtered.append(tool)
            break

        if not matched:
            filtered.append(tool)

    return filtered
