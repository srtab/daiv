from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

from automation.agent.toolkits import BaseToolkit

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import ToolFilter

logger = logging.getLogger("daiv.tools")


class MCPToolkit(BaseToolkit):
    """
    Toolkit for using MCP servers.
    """

    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        from automation.agent.mcp.registry import mcp_registry

        connections, tool_filters = mcp_registry.get_connections_and_filters()
        client = MultiServerMCPClient(connections, tool_name_prefix=True)

        try:
            tools = await client.get_tools()
        except Exception:
            logger.warning("Error getting tools from MCP servers.", exc_info=True)
            tools = []

        if tool_filters:
            tools = _apply_tool_filters(tools, tool_filters)

        # Handle tool errors and validation errors gracefully to allow the agent to continue
        for tool in tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True
            tool.tags = ["mcp_server"]
            tool.metadata = {"mcp_server": tool.name}

        return tools


def _apply_tool_filters(tools: list[BaseTool], filters: dict[str, ToolFilter]) -> list[BaseTool]:
    """
    Apply tool filters from MCP server configurations.

    Tools from MCP servers are prefixed with the server name (e.g., "sentry_find_organizations").
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
