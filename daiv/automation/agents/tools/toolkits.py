from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

from codebase.context import get_repository_ctx

from .editing import delete_tool, edit_tool, rename_tool, write_tool
from .navigation import glob_tool, grep_tool, ls_tool, read_tool
from .sandbox import bash_tool
from .web_search import web_search_tool

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


class FileNavigationToolkit(BaseToolkit):
    """
    Toolkit with tools to navigate files in codebases.
    """

    @classmethod
    def get_tools(cls) -> list[BaseTool]:
        return [glob_tool, grep_tool, ls_tool, read_tool]


class FileEditingToolkit(FileNavigationToolkit):
    """
    Toolkit for modifying files in codebases.
    """

    @classmethod
    def get_tools(cls) -> list[BaseTool]:
        return super().get_tools() + [write_tool, edit_tool, delete_tool, rename_tool]


class SandboxToolkit(BaseToolkit):
    """
    Toolkit for running bash commands in a sandbox environment.
    """

    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        config = get_repository_ctx().config

        if not config.commands.enabled:
            return []

        return [bash_tool]


class WebSearchToolkit(BaseToolkit):
    """
    Toolkit for performing web searches.
    """

    @classmethod
    def get_tools(cls) -> list[BaseTool]:
        return [web_search_tool]


class MCPToolkit(BaseToolkit):
    """
    Toolkit for using MCP servers.
    """

    @classmethod
    async def get_tools(cls) -> list[BaseTool]:
        from .mcp.registry import mcp_registry

        client = MultiServerMCPClient(mcp_registry.get_connections())

        try:
            tools = await client.get_tools()
        except ExceptionGroup:
            logger.warning("Error getting tools from MCP servers: Connection refused.")
            tools = []

        return tools
