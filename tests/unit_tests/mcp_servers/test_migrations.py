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
def test_malformed_json_logs_and_returns(tmp_path, monkeypatch, caplog):
    path = tmp_path / "mcp.json"
    path.write_text("{not valid json")
    monkeypatch.setattr("automation.agent.mcp.conf.settings.SERVERS_CONFIG_FILE", str(path))
    executor = MigrationExecutor(connection)
    executor.migrate([("mcp_servers", "0001_initial")])
    executor.loader.build_graph()
    executor = MigrationExecutor(connection)
    with caplog.at_level("ERROR"):
        executor.migrate([("mcp_servers", "0002_import_legacy_json")])

    from mcp_servers.models import MCPServer

    assert MCPServer.objects.filter(source="custom").count() == 0
    assert "Failed to read MCP servers config" in caplog.text


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
