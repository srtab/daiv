from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest


def migrate_to_head():
    """Migrate the mcp_servers app forward to its current leaf migration, so a real-model
    query runs against a schema that has every field the model declares. The leaf is derived
    dynamically (not hard-coded) so a future migration that adds a column won't rebreak these
    tests — they previously assumed 0004 was the leaf, and migration 0005 (new columns) broke
    every real-model access at a rolled-back state."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes("mcp_servers"))


def historical_mcpserver(migration: str):
    """The MCPServer model as it existed at ``migration`` state — it has no columns from later
    migrations, so a create/delete against a DB rolled back to that state works (the real model
    would reference not-yet-added columns)."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    state = executor.loader.project_state((("mcp_servers", migration),))
    return state.apps.get_model("mcp_servers", "MCPServer")


@pytest.fixture
def historical_at():
    """Roll mcp_servers back to a named migration, yield its historical model, and restore the
    schema to head afterwards — in a fixture so a failing assertion can't strand the whole suite
    at an old schema."""

    def _at(migration: str):
        MigrationExecutor(connection).migrate([("mcp_servers", migration)])
        return historical_mcpserver(migration)

    try:
        yield _at
    finally:
        migrate_to_head()


@pytest.fixture(autouse=True)
def _no_network_tool_sync(monkeypatch):
    """Neutralize the refresh view's network probe by default so view tests never
    open a real MCP connection. (Today ``sync_discovered_tools`` is called only by
    ``MCPServerRefreshToolsView``; a later task also calls it on save.)

    Tests that must assert *real* sync behavior opt out one of two ways:

    - per-test, by monkeypatching ``mcp_servers.views.services.sync_discovered_tools``
      (see the refresh-view tests in ``test_views.py``); the per-test patch wins for
      that test and is restored afterwards, or
    - module-wide, in ``test_services.py``, which calls ``services.sync_discovered_tools``
      *directly* and therefore defines its own identically-named autouse override fixture
      that no-ops. That override is NOT dead code — deleting it re-enables this stub for
      that module and silently regresses the two direct sync tests
      (``test_sync_discovered_tools_ok_persists_snapshot`` /
      ``..._failure_preserves_prior_snapshot``). Do not remove it without checking
      test_services.py."""
    from mcp_servers import services

    monkeypatch.setattr(services, "sync_discovered_tools", lambda server: {"ok": True, "count": 0})
