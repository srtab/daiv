from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_")

    FETCH_ENABLED: bool = Field(default=True, description="Whether to enable the Fetch MCP server")
    SENTRY_ENABLED: bool = Field(default=True, description="Whether to enable the Sentry MCP server")


settings = MCPSettings()  # type: ignore
