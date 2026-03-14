from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_", env_parse_none_str="None")

    PROXY_HOST: HttpUrl = Field(default=HttpUrl("http://mcp-proxy:9090"), description="The host of the MCP proxy")
    PROXY_ADDR: str = Field(default=":9090", description="The address the MCP proxy listens on.")
    PROXY_AUTH_TOKEN: SecretStr | None = Field(default=None, description="The auth token to the MCP proxy")

    # Sentry MCP server
    SENTRY_ENABLED: bool = Field(default=True, description="Whether to enable the Sentry MCP server")
    SENTRY_VERSION: str = Field(
        default="0.20.0",
        description="The version of the Sentry MCP server: https://www.npmjs.com/package/@sentry/mcp-server",
    )
    SENTRY_ACCESS_TOKEN: SecretStr | None = Field(default=None, description="The access token to the Sentry MCP server")
    SENTRY_HOST: str | None = Field(default=None, description="The host of the Sentry MCP server")

    # Context7 MCP server
    CONTEXT7_ENABLED: bool = Field(default=True, description="Whether to enable the Context7 MCP server")
    CONTEXT7_VERSION: str = Field(
        default="latest",
        description="The version of the Context7 MCP server: https://www.npmjs.com/package/@upstash/context7-mcp",
    )
    CONTEXT7_API_KEY: SecretStr | None = Field(default=None, description="The API key for the Context7 MCP server")


settings = MCPSettings()
