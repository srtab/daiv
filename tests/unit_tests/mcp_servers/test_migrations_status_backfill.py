import pytest
from mcp_servers.migrations import _status_backfill_ops as ops
from mcp_servers.models import MCPServer


@pytest.mark.django_db
def test_backfill_maps_enabled_to_status():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    on = MCPServer.objects.create(name="was-on", transport=MCPServer.Transport.HTTP, url="http://on", enabled=True)
    off = MCPServer.objects.create(name="was-off", transport=MCPServer.Transport.HTTP, url="http://off", enabled=False)
    # Pretend rows predate the field: force both to a wrong status, then run the backfill.
    MCPServer.objects.filter(pk__in=[on.pk, off.pk]).update(status=MCPServer.Status.ON_DEMAND)

    ops.backfill(MCPServer)

    on.refresh_from_db()
    off.refresh_from_db()
    assert on.status == MCPServer.Status.ACTIVE
    assert off.status == MCPServer.Status.DISABLED
