from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mcp_servers.seeds import BuiltinSeed

logger = logging.getLogger("daiv.mcp_servers")


def upsert_builtin_rows(seeds: Iterable[BuiltinSeed] | None = None) -> None:
    """Ensure each built-in seed has a DB row, creating missing ones with the
    full seed defaults (URL, description, tool filter, enabled state).

    Existing rows are never touched — the row is the source of truth once it
    exists (preserves admin edits and the enabled flag). The initial lookup
    guards against a missing/partially-migrated table (e.g. tests, a fresh DB)
    so the upsert is a no-op rather than an error in that state.
    """
    from django.db import IntegrityError, transaction

    from mcp_servers.models import MCPServer
    from mcp_servers.seeds import BUILTIN_SEEDS

    if seeds is None:
        seeds = BUILTIN_SEEDS

    try:
        existing = set(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).values_list("name", flat=True))
    except OperationalError, ProgrammingError:
        logger.warning("mcp_servers table not ready; skipping built-in upsert (run migrations).")
        return

    for seed in seeds:
        if seed.name in existing:
            continue
        try:
            # A nested atomic() isolates the failure to a savepoint: without it, a caller
            # running this inside its own atomic block (tests, a wrapped migrate) would have
            # every later query fail too, since an uncaught IntegrityError poisons the
            # enclosing transaction until it's rolled back to a savepoint.
            with transaction.atomic():
                MCPServer.objects.create(
                    name=seed.name,
                    description=seed.description,
                    source=MCPServer.Source.BUILTIN,
                    transport=MCPServer.Transport.HTTP,
                    url=seed.url,
                    tool_filter_mode=seed.tool_filter_mode,
                    tool_filter_items=list(seed.tool_filter_items),
                    enabled=seed.enabled,
                )
        except IntegrityError:
            logger.exception("Failed to upsert built-in MCP server row %r", seed.name)


def _on_post_migrate(sender, **kwargs):
    # Connected with ``sender=self`` below, so this only fires for this app's migrations.
    upsert_builtin_rows()


class MCPServersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mcp_servers"
    verbose_name = "MCP Servers"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        post_migrate.connect(_on_post_migrate, sender=self)
