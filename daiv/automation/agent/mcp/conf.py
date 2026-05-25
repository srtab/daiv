from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_", env_parse_none_str="None")

    SERVERS_CONFIG_FILE: str | None = Field(
        default=None,
        description=(
            "Deprecated: legacy path to the user MCP servers JSON config. "
            "Servers are now managed via the UI at /dashboard/mcp-servers/. "
            "Read once on first migration (0002_import_legacy_json) and ignored thereafter."
        ),
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


settings = MCPSettings()
