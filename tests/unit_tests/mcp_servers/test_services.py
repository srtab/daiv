from __future__ import annotations

import pytest
from mcp_servers.models import MCPServer
from mcp_servers.services import build_runtime_servers

from automation.agent.mcp.schemas import UserMcpServer


@pytest.mark.django_db
def test_returns_only_enabled_rows():
    MCPServer.objects.create(name="on", transport=MCPServer.Transport.HTTP, url="http://on", enabled=True)
    MCPServer.objects.create(name="off", transport=MCPServer.Transport.HTTP, url="http://off", enabled=False)
    out = build_runtime_servers()
    names = [dto_name for dto_name, _ in out]
    assert names == ["on"]


@pytest.mark.django_db
def test_literal_headers_decrypt_into_dto():
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[{"name": "Authorization", "mode": "literal", "value": "Bearer abc"}],
    )
    [(name, dto)] = build_runtime_servers()
    assert name == "srv"
    assert isinstance(dto, UserMcpServer)
    assert dto.headers == {"Authorization": "Bearer abc"}
    assert dto.type == "http"
    assert dto.url == "http://srv"


@pytest.mark.django_db
def test_env_ref_resolves_from_environment(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc-from-env")
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[{"name": "X-Token", "mode": "env_ref", "value": "MY_TOKEN"}],
    )
    [(_, dto)] = build_runtime_servers()
    assert dto.headers == {"X-Token": "abc-from-env"}


@pytest.mark.django_db
def test_missing_env_ref_drops_one_header_keeps_others(caplog, monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[
            {"name": "X-Keep", "mode": "literal", "value": "kept"},
            {"name": "X-Drop", "mode": "env_ref", "value": "MISSING_VAR"},
        ],
    )
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        [(_, dto)] = build_runtime_servers()
    assert dto.headers == {"X-Keep": "kept"}
    assert "MISSING_VAR" in caplog.text


@pytest.mark.django_db
def test_decryption_error_skips_row_keeps_others(caplog):
    # Build two rows; corrupt the ciphertext of the first.
    bad = MCPServer.objects.create(
        name="bad",
        transport=MCPServer.Transport.HTTP,
        url="http://bad",
        headers=[{"name": "X", "mode": "literal", "value": "secret"}],
    )
    MCPServer.objects.create(
        name="good",
        transport=MCPServer.Transport.HTTP,
        url="http://good",
        headers=[{"name": "Y", "mode": "literal", "value": "ok"}],
    )
    MCPServer.objects.filter(pk=bad.pk).update(_headers_encrypted="not-a-valid-fernet-token")

    with caplog.at_level("ERROR"):
        out = dict(build_runtime_servers())

    assert "bad" not in out
    assert "good" in out
    assert out["good"].headers == {"Y": "ok"}


@pytest.mark.django_db
def test_builtin_row_excluded_from_user_servers():
    # Built-ins are passed through the registry's own code path, not as
    # user-server DTOs. ``build_runtime_servers`` returns only ``source=custom``.
    MCPServer.objects.create(
        name="fake-builtin",
        source=MCPServer.Source.BUILTIN,
        transport=MCPServer.Transport.HTTP,
        url="http://db-stale-url",
        enabled=True,
    )
    out = build_runtime_servers()
    assert out == []


@pytest.mark.django_db
def test_disabled_builtin_row_excluded():
    MCPServer.objects.create(
        name="fake-builtin",
        source=MCPServer.Source.BUILTIN,
        transport=MCPServer.Transport.HTTP,
        url="http://db",
        enabled=False,
    )
    out = build_runtime_servers()
    assert out == []


async def test_test_connection_returns_tools_on_success(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from mcp_servers.services import test_connection

    fake_client = MagicMock()
    fake_tool = MagicMock()
    fake_tool.name = "demo_tool"
    fake_tool.description = "demo"
    fake_client.get_tools = AsyncMock(return_value=[fake_tool])

    monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: fake_client)

    result = await test_connection({"transport": "http", "url": "http://demo.test/mcp", "headers": []})
    assert result["ok"] is True
    assert [t["name"] for t in result["tools"]] == ["demo_tool"]


async def test_test_connection_reports_error(monkeypatch):
    from mcp_servers.services import test_connection

    def _fail(payload):
        raise RuntimeError("connect refused")

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})
    assert result["ok"] is False
    assert "connect refused" in result["error"]
