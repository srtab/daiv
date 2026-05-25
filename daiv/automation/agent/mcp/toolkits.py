from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

from automation.agent.toolkits import BaseToolkit

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import ToolFilter

logger = logging.getLogger("daiv.tools")


def _get_connection_url(conn) -> str:
    return getattr(conn, "url", "unknown")


class MCPToolkit(BaseToolkit):
    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        from asgiref.sync import sync_to_async
        from mcp_servers.services import build_runtime_servers

        from automation.agent.mcp.registry import mcp_registry

        user_servers = await sync_to_async(build_runtime_servers)()
        # Built-in ``is_enabled()`` hits the DB; marshal off the event loop.
        connections, tool_filters = await sync_to_async(mcp_registry.get_connections_and_filters)(user_servers)

        if not connections:
            return []

        server_urls = {name: _get_connection_url(conn) for name, conn in connections.items()}
        logger.debug("Connecting to MCP servers: %s", server_urls)

        # Fetch per server: a single failing endpoint must not blank tools from healthy peers.
        tools: list[BaseTool] = []
        for server_name, connection in connections.items():
            client = MultiServerMCPClient({server_name: connection}, tool_name_prefix=True)
            try:
                tools.extend(await client.get_tools())
            except Exception:
                logger.exception(
                    "Error getting tools from MCP server %r (%s)", server_name, _get_connection_url(connection)
                )

        if tool_filters:
            tools = _apply_tool_filters(tools, tool_filters)

        for tool in tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True
            tool.tags = ["mcp_server"]
            tool.metadata = {"mcp_server": tool.name}

        return tools


def _apply_tool_filters(tools: list[BaseTool], filters: dict[str, ToolFilter]) -> list[BaseTool]:
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
