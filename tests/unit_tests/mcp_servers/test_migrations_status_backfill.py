import pytest
from mcp_servers.migrations import _status_backfill_ops as ops


@pytest.mark.django_db(transaction=True)
def test_backfill_maps_enabled_to_status(historical_at):
    """Exercise the 0009 backfill against the historical model at 0008, where the legacy
    ``enabled`` column and the new ``status`` column coexist — ``enabled`` is dropped in 0010,
    so the live model can no longer reach this code path."""
    historical = historical_at("0008_mcpserver_status")

    historical.objects.all().delete()
    # Force a wrong status so the backfill's update is load-bearing, not a no-op.
    on = historical.objects.create(name="was-on", transport="http", url="http://on", enabled=True, status="on-demand")
    off = historical.objects.create(
        name="was-off", transport="http", url="http://off", enabled=False, status="on-demand"
    )

    ops.backfill(historical)

    on.refresh_from_db()
    off.refresh_from_db()
    assert on.status == "active"
    assert off.status == "disabled"


@pytest.mark.django_db(transaction=True)
def test_unbackfill_keeps_disabled_servers_off_on_rollback(historical_at):
    """Reversing 0010 re-adds ``enabled`` at its default (``True``). If 0009's reverse stayed a
    no-op, a rollback would turn every deliberately-disabled server back on."""
    historical = historical_at("0008_mcpserver_status")

    historical.objects.all().delete()
    # ``enabled=True`` on every row is exactly what reversing 0010 leaves behind.
    rows = {
        status: historical.objects.create(
            name=status, transport="http", url=f"http://{status}", enabled=True, status=status
        )
        for status in ("active", "on-demand", "disabled")
    }

    ops.unbackfill(historical)

    for row in rows.values():
        row.refresh_from_db()
    assert rows["active"].enabled is True
    assert rows["on-demand"].enabled is True
    assert rows["disabled"].enabled is False
