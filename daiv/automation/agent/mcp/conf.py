from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_", env_parse_none_str="None")

    SERVERS_CONFIG_FILE: str | None = Field(
        default=None, description="Path to the user MCP servers JSON config file (.mcp.json format)"
    )

    # Built-in MCP server URLs (supergateway containers). Set to None to disable.
    # Note: Docker Swarm normalizes service names with dashes to underscores.
    SENTRY_URL: str | None = Field(
        default="http://mcp_sentry:8000/mcp", description="The streamable HTTP URL of the Sentry supergateway container"
    )
    CONTEXT7_URL: str | None = Field(
        default="http://mcp_context7:8000/mcp",
        description="The streamable HTTP URL of the Context7 supergateway container",
    )

    DEFERRED_TOOLS_ENABLED: bool = Field(
        default=False, description="If True, MCP tools are deferred-loaded via tool_search instead of bound eagerly."
    )
    DEFERRED_TOOLS_TOP_K_DEFAULT: int = Field(
        default=5, description="Default number of search results returned by tool_search."
    )
    DEFERRED_TOOLS_TOP_K_MAX: int = Field(
        default=10, description="Maximum number of search results tool_search will return per call."
    )


settings = MCPSettings()
