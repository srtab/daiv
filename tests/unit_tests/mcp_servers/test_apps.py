from __future__ import annotations

import pytest
from mcp_servers.apps import upsert_builtin_rows
from mcp_servers.models import MCPServer


class _Stub:
    name = "stub"


@pytest.mark.django_db
def test_first_run_creates_row():
    upsert_builtin_rows([_Stub])
    row = MCPServer.objects.get(name="stub")
    assert row.source == MCPServer.Source.BUILTIN
    assert row.enabled is True


@pytest.mark.django_db
def test_subsequent_run_preserves_enabled_flag():
    upsert_builtin_rows([_Stub])
    MCPServer.objects.filter(name="stub").update(enabled=False)
    upsert_builtin_rows([_Stub])  # second pass
    assert MCPServer.objects.get(name="stub").enabled is False


@pytest.mark.django_db
def test_handles_missing_table_gracefully(monkeypatch):
    from django.db.utils import OperationalError

    def boom(*args, **kwargs):
        raise OperationalError("no such table")

    monkeypatch.setattr(MCPServer.objects, "filter", boom)
    # Should not raise — startup must not crash if migrations haven't run yet.
    upsert_builtin_rows([_Stub])


@pytest.mark.django_db
def test_db_enabled_returns_false_when_row_disabled():
    from automation.agent.mcp.base import MCPServer as Base

    class _SubBuiltin(Base):
        name = "togglable"

        def get_connection(self): ...

    MCPServer.objects.create(
        name="togglable",
        source=MCPServer.Source.BUILTIN,
        transport=MCPServer.Transport.HTTP,
        url="builtin://togglable",
        enabled=False,
    )
    assert _SubBuiltin._db_enabled() is False
    MCPServer.objects.filter(name="togglable").update(enabled=True)
    assert _SubBuiltin._db_enabled() is True
