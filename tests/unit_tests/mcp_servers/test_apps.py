from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError

import pytest
from mcp_servers.apps import upsert_builtin_rows
from mcp_servers.models import MCPServer
from mcp_servers.seeds import BUILTIN_SEEDS, BuiltinSeed

_STUB_SEED = BuiltinSeed(
    name="stub",
    description="stub server",
    url="https://stub.test/mcp",
    tool_filter_mode="allow",
    tool_filter_items=("one", "two"),
    enabled=False,
)


@pytest.mark.django_db
def test_first_run_creates_row_with_all_seed_fields():
    upsert_builtin_rows([_STUB_SEED])
    row = MCPServer.objects.get(name="stub")
    assert row.source == MCPServer.Source.BUILTIN
    assert row.url == "https://stub.test/mcp"
    assert row.description == "stub server"
    assert row.transport == MCPServer.Transport.HTTP
    assert row.tool_filter_mode == "allow"
    assert row.tool_filter_items == ["one", "two"]
    assert row.enabled is False


@pytest.mark.django_db
def test_defaults_to_builtin_seeds():
    upsert_builtin_rows()
    assert set(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).values_list("name", flat=True)) >= {
        s.name for s in BUILTIN_SEEDS
    }


@pytest.mark.django_db
def test_subsequent_run_never_touches_existing_rows():
    upsert_builtin_rows([_STUB_SEED])
    MCPServer.objects.filter(name="stub").update(enabled=True, url="https://edited.test/mcp")
    upsert_builtin_rows([_STUB_SEED])  # second pass
    row = MCPServer.objects.get(name="stub")
    assert row.enabled is True
    assert row.url == "https://edited.test/mcp"


@pytest.mark.django_db
@pytest.mark.parametrize("exc_cls", [OperationalError, ProgrammingError])
def test_handles_missing_table_gracefully(monkeypatch, exc_cls):
    def boom(*args, **kwargs):
        raise exc_cls("no such table")

    monkeypatch.setattr(MCPServer.objects, "filter", boom)
    upsert_builtin_rows([_STUB_SEED])


@pytest.mark.django_db
def test_name_collision_with_existing_row_is_caught(caplog):
    """A pre-existing row with the same name as a built-in seed (e.g. imported from
    legacy JSON under a name that later became a built-in, like 'sentry') must not
    crash post_migrate seeding — the unique-constraint IntegrityError is logged."""
    MCPServer.objects.create(name="stub", source=MCPServer.Source.CUSTOM, transport="http", url="http://x.test")
    with caplog.at_level("ERROR"):
        upsert_builtin_rows([_STUB_SEED])
    assert MCPServer.objects.filter(name="stub", source=MCPServer.Source.CUSTOM).exists()
    assert "Failed to upsert built-in MCP server row" in caplog.text
