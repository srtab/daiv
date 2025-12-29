from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

logger = logging.getLogger("daiv.tools")


class BaseToolkit(metaclass=ABCMeta):
    @classmethod
    @abstractmethod
    def get_tools(cls) -> list[BaseTool]:
        """
        Get the tools for the toolkit.
        """


class MCPToolkit(BaseToolkit):
    """
    Toolkit for using MCP servers.
    """

    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        from automation.agents.mcp.registry import mcp_registry

        client = MultiServerMCPClient(mcp_registry.get_connections())

        try:
            tools = await client.get_tools()
        except Exception:
            logger.warning("Error getting tools from MCP servers.", exc_info=True)
            tools = []

        # Handle tool errors and validation errors gracefully to allow the agent to continue
        for tool in tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True
            tool.tags = ["mcp_server"]
            tool.metadata = {"mcp_server": tool.name}

        return tools
