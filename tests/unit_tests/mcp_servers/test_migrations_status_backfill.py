from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest
from mcp_servers.migrations import _status_backfill_ops as ops


@pytest.mark.django_db(transaction=True)
def test_backfill_maps_enabled_to_status():
    """Exercise the 0009 backfill against the historical model at 0008, where the legacy
    ``enabled`` column and the new ``status`` column coexist — ``enabled`` is dropped in 0010,
    so the live model can no longer reach this code path."""
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0008_mcpserver_status")])
    state = executor.loader.project_state((("mcp_servers", "0008_mcpserver_status"),))
    Historical = state.apps.get_model("mcp_servers", "MCPServer")  # noqa: N806

    Historical.objects.all().delete()
    # Force a wrong status so the backfill's update is load-bearing, not a no-op.
    on = Historical.objects.create(name="was-on", transport="http", url="http://on", enabled=True, status="on-demand")
    off = Historical.objects.create(
        name="was-off", transport="http", url="http://off", enabled=False, status="on-demand"
    )

    ops.backfill(Historical)

    on.refresh_from_db()
    off.refresh_from_db()
    assert on.status == "active"
    assert off.status == "disabled"

    # Restore the schema to head so later tests see the real model.
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes("mcp_servers"))
