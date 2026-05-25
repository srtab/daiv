from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("daiv.mcp_servers")


def upsert_builtin_rows(builtin_classes: Iterable[type]) -> None:
    """Ensure each registered built-in MCP server has a DB row.

    Existing rows are not touched (preserves the admin's ``enabled`` choice).
    Missing rows are created with ``enabled=True``. Wrapped in a broad
    DB-exception catch so the app starts cleanly when migrations haven't
    run yet.
    """
    from mcp_servers.models import MCPServer

    try:
        existing = set(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).values_list("name", flat=True))
    except OperationalError, ProgrammingError:
        logger.warning("mcp_servers table not ready; skipping built-in upsert (run migrations).")
        return

    for cls in builtin_classes:
        if cls.name in existing:
            continue
        MCPServer.objects.create(
            name=cls.name,
            source=MCPServer.Source.BUILTIN,
            transport=MCPServer.Transport.HTTP,  # placeholder; overridden at runtime by the registry
            url="builtin://" + cls.name,  # placeholder; the runtime never reads this for built-ins
            enabled=True,
        )


class MCPServersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcp_servers"
    verbose_name = "MCP Servers"

    def ready(self) -> None:
        # Lazy import: the registry must finish collecting @mcp_server
        # classes from other apps' import-time decorators before we read it.
        from automation.agent.mcp.registry import mcp_registry

        upsert_builtin_rows(mcp_registry._registry)
