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

    # Deprecated: read once by migration 0004_materialize_builtin_rows (imported into the
    # corresponding MCPServer row) and by the startup deprecation warning. Never consumed at
    # runtime — edit the row at /dashboard/mcp-servers/ instead. Removal planned.
    # Removing these fields requires rewriting migration 0004 to read os.environ directly,
    # since it imports them from this module at migrate time.
    SENTRY_URL: str | None = Field(
        default="http://mcp_sentry:8000/mcp",
        description="Deprecated: legacy URL of the Sentry supergateway container (see above).",
    )
    CONTEXT7_URL: str | None = Field(
        default="http://mcp_context7:8000/mcp",
        description="Deprecated: legacy URL of the Context7 supergateway container (see above).",
    )

    TOOL_LOAD_TIMEOUT: float = Field(
        default=30.0,
        description=(
            "Max seconds to wait for a single MCP server to return its tools. A server that exceeds "
            "this (e.g. a broken handshake) is skipped so it cannot freeze chats and runs."
        ),
    )


settings = MCPSettings()
