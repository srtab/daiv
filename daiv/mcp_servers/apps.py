from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger("daiv.mcp_servers")

_LEGACY_WARNED = False


def upsert_builtin_rows(builtin_names: Iterable[str]) -> None:
    """Ensure each registered built-in MCP server has a DB row.

    Existing rows are not touched (preserves the admin's ``enabled`` choice).
    Missing rows are created with ``enabled=True``. The initial lookup guards
    against a missing/partially-migrated table (e.g. tests, a fresh DB) so the
    upsert is a no-op rather than an error in that state.
    """
    from django.db import IntegrityError

    from mcp_servers.models import MCPServer

    try:
        existing = set(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).values_list("name", flat=True))
    except OperationalError, ProgrammingError:
        logger.warning("mcp_servers table not ready; skipping built-in upsert (run migrations).")
        return

    for name in builtin_names:
        if name in existing:
            continue
        try:
            # transport/url are schema-required but unused for built-ins (they supply their own Connection).
            MCPServer.objects.create(
                name=name,
                source=MCPServer.Source.BUILTIN,
                transport=MCPServer.Transport.HTTP,
                url="builtin://" + name,
                enabled=True,
            )
        except IntegrityError:
            logger.exception("Failed to upsert built-in MCP server row %r", name)


def warn_legacy_env_if_present() -> None:
    global _LEGACY_WARNED
    if _LEGACY_WARNED:
        return
    from automation.agent.mcp.conf import settings as mcp_conf
    from mcp_servers.models import MCPServer

    if not mcp_conf.SERVERS_CONFIG_FILE:
        return
    try:
        any_rows = MCPServer.objects.exists()
    except OperationalError, ProgrammingError:
        return
    if not any_rows:
        return
    logger.warning(
        "MCP_SERVERS_CONFIG_FILE is deprecated; servers are now managed via the UI at "
        "/dashboard/mcp-servers/. Unset MCP_SERVERS_CONFIG_FILE to silence this warning."
    )
    _LEGACY_WARNED = True


def _on_post_migrate(sender, **kwargs):
    # Connected with ``sender=self`` below, so this only fires for this app's migrations.
    from automation.agent.mcp.registry import mcp_registry

    upsert_builtin_rows(mcp_registry.builtin_names())
    warn_legacy_env_if_present()


class MCPServersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcp_servers"
    verbose_name = "MCP Servers"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_on_post_migrate, sender=self)
