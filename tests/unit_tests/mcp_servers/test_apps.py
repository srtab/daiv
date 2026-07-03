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
def test_warns_when_deprecated_url_env_var_is_set(monkeypatch, caplog):
    import mcp_servers.apps as apps_mod

    monkeypatch.setattr(apps_mod, "_LEGACY_WARNED", False)
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", None)
    monkeypatch.setenv("MCP_SENTRY_URL", "http://mcp_sentry:8000/mcp")
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        apps_mod.warn_legacy_env_if_present()
    assert "MCP_SENTRY_URL" in caplog.text
    assert "deprecated" in caplog.text.lower()


@pytest.mark.django_db
def test_no_url_var_warning_when_unset(monkeypatch, caplog):
    import mcp_servers.apps as apps_mod

    monkeypatch.setattr(apps_mod, "_LEGACY_WARNED", False)
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", None)
    monkeypatch.delenv("MCP_SENTRY_URL", raising=False)
    monkeypatch.delenv("MCP_CONTEXT7_URL", raising=False)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        apps_mod.warn_legacy_env_if_present()
    assert caplog.records == []


@pytest.mark.django_db
def test_warns_once_when_env_var_still_set_and_rows_exist(monkeypatch, caplog):
    import mcp_servers.apps as apps_mod
    from mcp_servers.models import MCPServer

    # Reset the module-level "already warned" sentinel
    monkeypatch.setattr(apps_mod, "_LEGACY_WARNED", False)
    MCPServer.objects.create(name="x", transport="http", url="http://x.test")
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", "/etc/legacy.json")
    monkeypatch.delenv("MCP_SENTRY_URL", raising=False)
    monkeypatch.delenv("MCP_CONTEXT7_URL", raising=False)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        apps_mod.warn_legacy_env_if_present()
        apps_mod.warn_legacy_env_if_present()  # second call should not re-log
    assert sum("deprecated" in r.message.lower() for r in caplog.records) == 1


@pytest.mark.django_db
def test_no_warning_when_env_var_unset(monkeypatch, caplog):
    import mcp_servers.apps as apps_mod
    from mcp_servers.models import MCPServer

    monkeypatch.setattr(apps_mod, "_LEGACY_WARNED", False)
    MCPServer.objects.create(name="x", transport="http", url="http://x.test")
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", None)
    monkeypatch.delenv("MCP_SENTRY_URL", raising=False)
    monkeypatch.delenv("MCP_CONTEXT7_URL", raising=False)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        apps_mod.warn_legacy_env_if_present()
    assert caplog.records == []
