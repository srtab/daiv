from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_", env_parse_none_str="None")

    SERVERS_CONFIG_FILE: str | None = Field(
        default=None, description="Path to the user MCP servers JSON config file (.mcp.json format)"
    )

    # Built-in MCP server URLs (supergateway containers). Set to None to disable.
    SENTRY_URL: str | None = Field(
        default="http://mcp-sentry:8000/sse", description="The SSE URL of the Sentry supergateway container"
    )
    CONTEXT7_URL: str | None = Field(
        default="http://mcp-context7:8000/sse", description="The SSE URL of the Context7 supergateway container"
    )


settings = MCPSettings()
