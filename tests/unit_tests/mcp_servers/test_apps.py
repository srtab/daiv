from __future__ import annotations

from django.db.utils import OperationalError, ProgrammingError

import pytest
from mcp_servers.apps import upsert_builtin_rows
from mcp_servers.models import MCPServer


@pytest.mark.django_db
def test_first_run_creates_row():
    upsert_builtin_rows(["stub"])
    row = MCPServer.objects.get(name="stub")
    assert row.source == MCPServer.Source.BUILTIN
    assert row.enabled is True


@pytest.mark.django_db
def test_subsequent_run_preserves_enabled_flag():
    upsert_builtin_rows(["stub"])
    MCPServer.objects.filter(name="stub").update(enabled=False)
    upsert_builtin_rows(["stub"])  # second pass
    assert MCPServer.objects.get(name="stub").enabled is False


@pytest.mark.django_db
@pytest.mark.parametrize("exc_cls", [OperationalError, ProgrammingError])
def test_handles_missing_table_gracefully(monkeypatch, exc_cls):
    def boom(*args, **kwargs):
        raise exc_cls("no such table")

    monkeypatch.setattr(MCPServer.objects, "filter", boom)
    upsert_builtin_rows(["stub"])


@pytest.mark.django_db
def test_db_enabled_propagates_operational_error(monkeypatch):
    """OperationalError must propagate — swallowing it would flip every disabled built-in on during an outage."""
    from automation.agent.mcp.base import MCPServer as Base

    class _Sub(Base):
        name = "outage-stub"

        def get_connection(self): ...

    def boom(*args, **kwargs):
        raise OperationalError("connection refused")

    monkeypatch.setattr(MCPServer.objects, "filter", boom)
    with pytest.raises(OperationalError):
        _Sub._db_enabled()


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


@pytest.mark.django_db
def test_warns_once_when_env_var_still_set_and_rows_exist(monkeypatch, caplog):
    import mcp_servers.apps as apps_mod
    from mcp_servers.models import MCPServer

    # Reset the module-level "already warned" sentinel
    monkeypatch.setattr(apps_mod, "_LEGACY_WARNED", False)
    MCPServer.objects.create(name="x", transport="http", url="http://x.test")
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", "/etc/legacy.json")
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
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        apps_mod.warn_legacy_env_if_present()
    assert caplog.records == []
