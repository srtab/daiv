"""
Pydantic models for MCP (Model Context Protocol) configuration.

This module defines the schema for configuring MCP servers with support for
different transport types (sse, streamable-http) and user-defined MCP servers.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolFilter(BaseModel):
    """Tool filtering configuration for MCP servers."""

    model_config = ConfigDict(populate_by_name=True)

    mode: Literal["allow", "block"] = Field(
        ..., description="Filtering mode - 'allow' to whitelist tools, 'block' to blacklist tools"
    )
    items: list[str] = Field(..., alias="list", description="List of tool names to filter based on the mode")


class UserMcpServer(BaseModel):
    """User-defined MCP server from config file (Claude Code .mcp.json format)."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["sse", "http"]
    url: str
    headers: dict[str, str] | None = None
    tool_filter: ToolFilter | None = Field(None, alias="toolFilter")


class UserMcpServersConfig(BaseModel):
    """Root model for user-defined MCP servers config file."""

    model_config = ConfigDict(populate_by_name=True)

    mcp_servers: dict[str, UserMcpServer] = Field(default_factory=dict, alias="mcpServers")
