from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="MCP_", env_parse_none_str="None")

    # The legacy MCP_SERVERS_CONFIG_FILE / MCP_SENTRY_URL / MCP_CONTEXT7_URL settings were
    # removed: MCP servers are managed as DB rows via the UI at /dashboard/mcp-servers/. The
    # one-shot data migrations (0002_import_legacy_json, 0004_materialize_builtin_rows) still
    # honour those env vars, but read them directly from os.environ / /run/secrets at migrate
    # time so nothing depends on this module for them.
    TOOL_LOAD_TIMEOUT: float = Field(
        default=30.0,
        description=(
            "Max seconds to wait for a single MCP server to return its tools. A server that exceeds "
            "this (e.g. a broken handshake) is skipped so it cannot freeze chats and runs."
        ),
    )


settings = MCPSettings()
