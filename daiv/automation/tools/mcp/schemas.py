"""
Pydantic models for MCP (Model Context Protocol) configuration.

This module defines the schema for configuring MCP proxy servers and MCP servers
with support for different transport types (stdio, sse, streamable-http).
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ToolFilter(BaseModel):
    """Tool filtering configuration for MCP servers."""

    mode: Literal["allow", "block"] = Field(
        ..., description="Filtering mode - 'allow' to whitelist tools, 'block' to blacklist tools"
    )
    items: list[str] = Field(..., alias="list", description="List of tool names to filter based on the mode")

    class Config:
        validate_by_name = True


class CommonOptions(BaseModel):
    """Common options for both mcpProxy and mcpServers."""

    panic_if_invalid: bool | None = Field(
        None, alias="panicIfInvalid", description="If true, the server will panic if the client is invalid"
    )
    log_enabled: bool | None = Field(
        None, alias="logEnabled", description="If true, the server will log the client's requests"
    )
    auth_tokens: list[str] | None = Field(
        None, alias="authTokens", description="List of authentication tokens for the client"
    )
    tool_filter: ToolFilter | None = Field(
        None, alias="toolFilter", description="Tool filtering configuration (only effective in mcpServers)"
    )

    class Config:
        validate_by_name = True


class McpProxyConfig(BaseModel):
    """Configuration for the MCP proxy server."""

    base_url: str = Field(..., alias="baseURL", description="The public accessible URL of the server")
    addr: str = Field(..., description="The address the server listens on")
    name: str = Field(..., description="The name of the server")
    version: str = Field(..., description="The version of the server")
    options: CommonOptions | None = Field(None, description="Default options for the mcpServers")

    class Config:
        validate_by_name = True


class StdioMcpServer(BaseModel):
    """Configuration for stdio-based MCP servers."""

    command: str = Field(..., description="The command to run the MCP client")
    args: list[str] | None = Field(None, description="The arguments to pass to the command")
    env: dict[str, str] | None = Field(None, description="The environment variables to set for the command")
    options: CommonOptions | None = Field(None, description="Options specific to the client")


class SseMcpServer(BaseModel):
    """Configuration for SSE-based MCP servers."""

    url: str = Field(..., description="The URL of the MCP client")
    headers: dict[str, str] | None = Field(None, description="The headers to send with the request to the MCP client")
    options: CommonOptions | None = Field(None, description="Options specific to the client")


class StreamableHttpMcpServer(BaseModel):
    """Configuration for streamable HTTP-based MCP servers."""

    transport_type: Literal["streamable-http"] = Field(
        "streamable-http", alias="transportType", description="Must be explicitly set to 'streamable-http'"
    )
    url: str = Field(..., description="The URL of the MCP client")
    headers: dict[str, str] | None = Field(None, description="The headers to send with the request to the MCP client")
    timeout: int | None = Field(None, description="The timeout for the request to the MCP client")
    options: CommonOptions | None = Field(None, description="Options specific to the client")

    class Config:
        validate_by_name = True


class McpConfiguration(BaseModel):
    """Root configuration model for MCP proxy and servers."""

    mcp_proxy: McpProxyConfig = Field(..., alias="mcpProxy", description="Proxy HTTP server configuration")
    mcp_servers: dict[str, StdioMcpServer | SseMcpServer | StreamableHttpMcpServer] = Field(
        ..., alias="mcpServers", description="MCP server configurations indexed by server name"
    )

    class Config:
        validate_by_name = True

    @field_validator("mcp_servers")
    @classmethod
    def validate_server_names(cls, v):
        """Validate that server names are not empty and are valid identifiers."""
        for server_name in v:
            if not server_name or not server_name.strip():
                raise ValueError("Server names cannot be empty")
            # Additional validation could be added here for allowed characters, etc.
        return v

    @staticmethod
    def populate():
        """
        Populate the MCP configuration with the current settings.
        """

        from .conf import settings
        from .registry import mcp_registry

        auth_tokens = [settings.MCP_PROXY_AUTH_TOKEN.get_secret_value()] if settings.MCP_PROXY_AUTH_TOKEN else []

        return McpConfiguration(
            mcp_proxy=McpProxyConfig(
                base_url=settings.MCP_PROXY_HOST.encoded_string(),
                addr=settings.MCP_PROXY_ADDR,
                name="daiv-mcp-proxy",
                version="0.1.0",
                options=CommonOptions(auth_tokens=auth_tokens, panic_if_invalid=False, log_enabled=True),
            ),
            mcp_servers=mcp_registry.get_mcp_servers_config(),
        )
