from __future__ import annotations

from django.apps import AppConfig


class MCPServersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcp_servers"
    verbose_name = "MCP Servers"

    def ready(self) -> None:
        # Built-in upsert is wired in Task 9. Keep this stub so Django picks
        # up the app cleanly until then.
        return None
