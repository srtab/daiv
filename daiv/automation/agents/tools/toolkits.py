from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from typing import TYPE_CHECKING

from langchain_mcp_adapters.client import MultiServerMCPClient

from .editing import delete_tool, edit_tool, rename_tool, write_tool
from .merge_request import job_logs_tool, pipeline_tool
from .navigation import glob_tool, grep_tool, ls_tool, read_tool
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


class FileEditingToolkit(BaseToolkit):
    """
    Toolkit for modifying files in codebases.
    """

    @classmethod
    def get_tools(cls) -> list[BaseTool]:
        return [write_tool, edit_tool, delete_tool, rename_tool]


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
        from .mcp.interceptors import ToolCallInterceptor
        from .mcp.registry import mcp_registry

        client = MultiServerMCPClient(mcp_registry.get_connections(), tool_interceptors=[ToolCallInterceptor()])

        try:
            tools = await client.get_tools()
        except Exception:
            logger.warning("Error getting tools from MCP servers: Connection refused.", exc_info=True)
            tools = []

        return tools


class MergeRequestToolkit(BaseToolkit):
    """
    Toolkit with tools to inspect merge request pipelines and job logs.
    """

    @classmethod
    def get_tools(cls) -> list[BaseTool]:
        """
        Get the tools for the toolkit.

        Returns:
            list[BaseTool]: List of merge request inspection tools.
        """
        return [pipeline_tool, job_logs_tool]
