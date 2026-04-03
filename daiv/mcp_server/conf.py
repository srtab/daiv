from pydantic_settings import BaseSettings, SettingsConfigDict


class McpServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCP_SERVER_")

    ENABLED: bool = True


settings = McpServerSettings()
