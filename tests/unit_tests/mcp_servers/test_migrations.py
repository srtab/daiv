from __future__ import annotations

import json

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest


def _populate_config_file(tmp_path, monkeypatch, payload: dict) -> str:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", str(path))
    return str(path)


@pytest.mark.django_db(transaction=True)
def test_import_creates_rows_from_json(tmp_path, monkeypatch):
    _populate_config_file(
        tmp_path,
        monkeypatch,
        {
            "mcpServers": {
                "demo": {
                    "type": "http",
                    "url": "http://demo.test/mcp",
                    "headers": {"X-Token": "${API_TOKEN}", "X-Plain": "no-ref-here"},
                    "toolFilter": {"mode": "allow", "list": ["search", "list"]},
                }
            }
        },
    )

    executor = MigrationExecutor(connection)
    # Roll back to 0001 so 0002 runs fresh (test DB is created with all migrations applied)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.get(name="demo")
    assert obj.transport == "http"
    assert obj.url == "http://demo.test/mcp"
    assert obj.tool_filter_mode == "allow"
    assert obj.tool_filter_items == ["search", "list"]
    assert obj.headers == [
        {"name": "X-Token", "mode": "env_ref", "value": "API_TOKEN"},
        {"name": "X-Plain", "mode": "literal", "value": "no-ref-here"},
    ]


@pytest.mark.django_db(transaction=True)
def test_import_is_idempotent(tmp_path, monkeypatch):
    _populate_config_file(tmp_path, monkeypatch, {"mcpServers": {"demo": {"type": "http", "url": "http://demo.test"}}})
    # First pass: roll back and apply 0002 (imports the server row)
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0002_import_legacy_json")])
    # Second pass: roll back to 0001 then re-apply 0002 (noop_reverse leaves data intact;
    # import function skips existing names → still exactly 1 row)
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0002_import_legacy_json")])
    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(name="demo").count() == 1


@pytest.mark.django_db(transaction=True)
def test_import_normalizes_invalid_legacy_name(tmp_path, monkeypatch, caplog):
    """Legacy keys with underscores/uppercase are normalized to a valid slug on import.
    Importing them verbatim (``objects.create`` bypasses validators) would create a row the
    edit form can never re-save, since ``name`` is re-validated against MCP_NAME_RE on save."""
    _populate_config_file(
        tmp_path, monkeypatch, {"mcpServers": {"My_Server": {"type": "http", "url": "http://d.test"}}}
    )
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("WARNING"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.constants import MCP_NAME_RE
    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.get(source="custom")
    assert obj.name == "my-server"
    assert MCP_NAME_RE.match(obj.name)  # the crux: the row is now re-savable through the edit form
    assert obj.url == "http://d.test"
    assert "Renaming legacy MCP server" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_import_skips_unnormalizable_legacy_name(tmp_path, monkeypatch, caplog):
    """A legacy key that yields nothing valid after normalization is skipped loudly rather
    than creating a row that violates the slug validator."""
    _populate_config_file(tmp_path, monkeypatch, {"mcpServers": {"___": {"type": "http", "url": "http://d.test"}}})
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("WARNING"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(source="custom").count() == 0
    assert "cannot be normalized" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_env_ref_only_for_exact_dollar_brace_match(tmp_path, monkeypatch, caplog):
    """Mixed strings like ``Bearer ${TOKEN}`` must stay literal — the new model can't preserve the prefix."""
    _populate_config_file(
        tmp_path,
        monkeypatch,
        {
            "mcpServers": {
                "demo": {
                    "type": "http",
                    "url": "http://demo.test",
                    "headers": {"Authorization": "Bearer ${API_TOKEN}", "X-Pure": "${PURE_VAR}"},
                }
            }
        },
    )
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("WARNING"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.get(name="demo")
    headers_by_name = {h["name"]: h for h in obj.headers}
    assert headers_by_name["Authorization"] == {
        "name": "Authorization",
        "mode": "literal",
        "value": "Bearer ${API_TOKEN}",
    }
    assert headers_by_name["X-Pure"] == {"name": "X-Pure", "mode": "env_ref", "value": "PURE_VAR"}
    assert "Authorization" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_missing_file_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", str(tmp_path / "missing.json"))
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(source="custom").count() == 0


@pytest.mark.django_db(transaction=True)
def test_malformed_json_aborts_migration(tmp_path, monkeypatch, caplog):
    """Unreadable/corrupt legacy config must fail the migration loudly, not silently
    drop every custom server with only a log line as evidence (this is the one-shot,
    non-reversible import — nothing else ever re-reads the file)."""
    path = tmp_path / "mcp.json"
    path.write_text("{not valid json")
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", str(path))
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("ERROR"), pytest.raises(json.JSONDecodeError):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    assert "Failed to read MCP servers config" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_unexpected_parse_error_propagates(tmp_path, monkeypatch):
    """A bug during parsing (not a legacy-config validation issue) must fail the
    migration loudly instead of being misreported as bad legacy config."""
    _populate_config_file(tmp_path, monkeypatch, {"mcpServers": {}})
    monkeypatch.setattr(
        "automation.agent.mcp.schemas.UserMcpServersConfig.model_validate",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with pytest.raises(RuntimeError):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])


@pytest.mark.django_db(transaction=True)
def test_schema_validation_failure_logs_and_returns(tmp_path, monkeypatch, caplog):
    # websocket isn't a valid transport
    _populate_config_file(tmp_path, monkeypatch, {"mcpServers": {"demo": {"type": "websocket", "url": "ws://x"}}})
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("ERROR"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(source="custom").count() == 0
    assert "Invalid MCP servers config" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_empty_filter_list_normalized_to_none_survives_constraint(tmp_path, monkeypatch, caplog):
    """A legacy non-'none' filter mode with an empty list must be normalized to 'none' on
    import, otherwise applying the 0003 check constraint would abort the migration."""
    _populate_config_file(
        tmp_path,
        monkeypatch,
        {
            "mcpServers": {
                "demo": {"type": "http", "url": "http://demo.test", "toolFilter": {"mode": "allow", "list": []}}
            }
        },
    )
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("WARNING"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])
    # Applying the constraint migration must not raise (the row is now mode='none').
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0003_mcpserver_mcp_tool_filter_items_required_when_mode_set")])

    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.get(name="demo")
    assert obj.tool_filter_mode == "none"
    assert obj.tool_filter_items == []
    assert "importing as 'none'" in caplog.text


@pytest.mark.django_db(transaction=True)
def test_default_fallback_syntax_logs_warning(tmp_path, monkeypatch, caplog):
    _populate_config_file(
        tmp_path,
        monkeypatch,
        {
            "mcpServers": {
                "demo": {"type": "http", "url": "http://demo.test", "headers": {"X-Tok": "${API:-default-value}"}}
            }
        },
    )
    executor = MigrationExecutor(connection)
    # Roll back to 0001 so 0002 runs fresh (test DB is created with all migrations applied)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("WARNING"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    obj = MCPServer.objects.get(name="demo")
    assert obj.headers == [{"name": "X-Tok", "mode": "env_ref", "value": "API"}]
    assert "default-value" in caplog.text  # warned about dropped fallback


def _rollback_and_seed_placeholder(name: str, *, enabled: bool = True):
    """Roll back to 0003 and put a pre-materialisation placeholder row in place."""
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0003_mcpserver_mcp_tool_filter_items_required_when_mode_set")])

    from mcp_servers.models import MCPServer

    MCPServer.objects.all().delete()
    MCPServer.objects.create(name=name, source="builtin", transport="http", url=f"builtin://{name}", enabled=enabled)


def _apply_0004():
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0004_materialize_builtin_rows")])


@pytest.mark.django_db(transaction=True)
def test_0004_placeholder_gets_effective_env_url_and_legacy_filter(monkeypatch):
    _rollback_and_seed_placeholder("sentry", enabled=True)
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SENTRY_URL", "http://mcp-sentry:8000/mcp")
    _apply_0004()

    from mcp_servers.models import MCPServer

    row = MCPServer.objects.get(name="sentry")
    assert row.url == "http://mcp-sentry:8000/mcp"
    assert row.enabled is True  # preserved
    assert row.transport == "http"
    assert row.tool_filter_mode == "allow"
    assert "search_issue" in row.tool_filter_items  # legacy stdio name
    assert "find_teams" in row.tool_filter_items
    assert row.description  # non-empty


@pytest.mark.django_db(transaction=True)
def test_0004_none_kill_switch_maps_to_remote_default_disabled(monkeypatch):
    _rollback_and_seed_placeholder("sentry", enabled=True)
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SENTRY_URL", None)
    _apply_0004()

    from mcp_servers.models import MCPServer

    row = MCPServer.objects.get(name="sentry")
    assert row.url.startswith("https://mcp.sentry.dev/mcp")
    assert row.enabled is False
    assert "search_issues" in row.tool_filter_items  # hosted-endpoint name
    assert "search_issue" not in row.tool_filter_items


@pytest.mark.django_db(transaction=True)
def test_0004_preserves_disabled_flag(monkeypatch):
    _rollback_and_seed_placeholder("context7", enabled=False)
    monkeypatch.setattr("automation.agent.mcp.conf.settings.CONTEXT7_URL", "http://mcp_context7:8000/mcp")
    _apply_0004()

    from mcp_servers.models import MCPServer

    row = MCPServer.objects.get(name="context7")
    assert row.url == "http://mcp_context7:8000/mcp"
    assert row.enabled is False


@pytest.mark.django_db(transaction=True)
def test_0004_skips_already_materialized_rows(monkeypatch):
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0003_mcpserver_mcp_tool_filter_items_required_when_mode_set")])

    from mcp_servers.models import MCPServer

    MCPServer.objects.all().delete()
    MCPServer.objects.create(
        name="sentry", source="builtin", transport="http", url="https://my-bridge.internal/mcp", enabled=True
    )
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SENTRY_URL", "http://mcp_sentry:8000/mcp")
    _apply_0004()

    row = MCPServer.objects.get(name="sentry")
    assert row.url == "https://my-bridge.internal/mcp"  # untouched


@pytest.mark.django_db(transaction=True)
def test_0004_missing_rows_are_not_created():
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0003_mcpserver_mcp_tool_filter_items_required_when_mode_set")])

    from mcp_servers.models import MCPServer

    MCPServer.objects.all().delete()
    _apply_0004()
    assert MCPServer.objects.count() == 0  # fresh install: the post_migrate upsert seeds instead
