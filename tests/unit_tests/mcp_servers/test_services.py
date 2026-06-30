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


@pytest.mark.django_db
def test_tool_filter_round_trips_through_dto():
    from automation.agent.mcp.schemas import ToolFilter

    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv.test",
        tool_filter_mode=MCPServer.FilterMode.ALLOW,
        tool_filter_items=["alpha", "beta"],
    )
    [(_, dto)] = build_runtime_servers()
    assert isinstance(dto.tool_filter, ToolFilter)
    assert dto.tool_filter.mode == "allow"
    assert dto.tool_filter.items == ["alpha", "beta"]


@pytest.mark.django_db
def test_tool_filter_none_when_mode_is_none():
    MCPServer.objects.create(
        name="srv2",
        transport=MCPServer.Transport.HTTP,
        url="http://srv2.test",
        tool_filter_mode=MCPServer.FilterMode.NONE,
        tool_filter_items=["foo"],  # ignored when mode is NONE
    )
    [(_, dto)] = build_runtime_servers()
    assert dto.tool_filter is None


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
    assert "RuntimeError" in result["error"]


async def test_test_connection_reports_blank_exception_with_class_name(monkeypatch):
    """str(err) is empty for many httpx/asyncio exceptions — error must still surface the class name."""
    from mcp_servers.services import test_connection

    class _SilentError(Exception):
        def __str__(self) -> str:
            return ""

    def _fail(payload):
        raise _SilentError

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})
    assert result["ok"] is False
    assert result["error"]
    assert "_SilentError" in result["error"]


async def test_discover_tools_returns_empty_on_failure(monkeypatch, caplog):
    from mcp_servers.models import MCPServer
    from mcp_servers.services import discover_tools

    async def _fail(payload):
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr("mcp_servers.services.test_connection", _fail)
    server = MCPServer(name="x", transport=MCPServer.Transport.HTTP, url="http://x.test")
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        tools = await discover_tools(server)
    assert tools == []
    assert "Tool discovery failed" in caplog.text


async def test_discover_tools_propagates_decryption_error(monkeypatch):
    """Propagated so views can show a key-rotation error instead of 500-ing."""
    from mcp_servers.services import discover_tools

    from core.encryption import DecryptionError

    class _Stub:
        name = "broken"
        transport = "http"
        url = "http://broken.test"

        def is_builtin(self) -> bool:
            return False

        @property
        def headers(self):
            raise DecryptionError("key rotated")

    with pytest.raises(DecryptionError):
        await discover_tools(_Stub())


@pytest.mark.django_db
def test_server_health_ok_for_resolved_env_ref(monkeypatch):
    from mcp_servers.models import MCPServer
    from mcp_servers.services import server_health

    monkeypatch.setenv("PRESENT_VAR", "v")
    s = MCPServer.objects.create(
        name="a",
        transport=MCPServer.Transport.HTTP,
        url="http://a.test",
        headers=[{"name": "X-T", "mode": "env_ref", "value": "PRESENT_VAR"}],
    )
    assert server_health(s) == {"ok": True, "reason": None}


@pytest.mark.django_db
def test_server_health_flags_missing_env_ref(monkeypatch):
    from mcp_servers.models import MCPServer
    from mcp_servers.services import server_health

    monkeypatch.delenv("MISSING_VAR", raising=False)
    s = MCPServer.objects.create(
        name="b",
        transport=MCPServer.Transport.HTTP,
        url="http://b.test",
        headers=[{"name": "X-T", "mode": "env_ref", "value": "MISSING_VAR"}],
    )
    health = server_health(s)
    assert health["ok"] is False
    assert "MISSING_VAR" in health["reason"]


@pytest.mark.django_db
def test_server_health_flags_undecryptable_headers():
    from mcp_servers.models import MCPServer
    from mcp_servers.services import server_health

    s = MCPServer.objects.create(
        name="c",
        transport=MCPServer.Transport.HTTP,
        url="http://c.test",
        headers=[{"name": "X-T", "mode": "literal", "value": "secret"}],
    )
    MCPServer.objects.filter(pk=s.pk).update(_headers_encrypted="not-a-fernet-token")
    s.refresh_from_db()
    health = server_health(s)
    assert health["ok"] is False
    assert "decrypt" in health["reason"].lower()


def test_build_client_maps_http_transport_and_resolves_env_ref(monkeypatch):
    """``_build_client`` is the only place a form's transport string becomes a real
    Connection — exercise it directly rather than through a mock."""
    from mcp_servers.services import _build_client

    monkeypatch.setenv("TOK", "from-env")
    client = _build_client({
        "transport": "http",
        "url": "http://demo.test/mcp",
        "headers": [
            {"name": "X-Lit", "mode": "literal", "value": "lit"},
            {"name": "X-Env", "mode": "env_ref", "value": "TOK"},
        ],
    })
    conn = client.connections["__probe__"]
    assert conn["transport"] == "streamable_http"
    assert conn["url"] == "http://demo.test/mcp"
    assert conn["headers"] == {"X-Lit": "lit", "X-Env": "from-env"}


def test_build_client_maps_sse_transport():
    from mcp_servers.services import _build_client

    client = _build_client({"transport": "sse", "url": "http://demo.test/sse", "headers": []})
    conn = client.connections["__probe__"]
    assert conn["transport"] == "sse"
    assert conn["url"] == "http://demo.test/sse"
    # No headers → None, not an empty dict.
    assert conn["headers"] is None


def test_build_client_rejects_unknown_transport():
    from mcp_servers.services import _build_client

    with pytest.raises(ValueError, match="Unsupported transport"):
        _build_client({"transport": "carrier-pigeon", "url": "http://x.test", "headers": []})


def test_build_client_warns_on_missing_env_ref(caplog, monkeypatch):
    """Test-connection resolution must warn on a missing env var, matching the
    runtime adapter (the two paths share ``_resolve_header_entries``)."""
    from mcp_servers.services import _build_client

    monkeypatch.delenv("ABSENT", raising=False)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        client = _build_client({
            "transport": "http",
            "url": "http://x.test",
            "headers": [{"name": "X-Env", "mode": "env_ref", "value": "ABSENT"}],
        })
    assert client.connections["__probe__"]["headers"] is None
    assert "ABSENT" in caplog.text


@pytest.mark.django_db
def test_build_runtime_servers_drops_header_with_unknown_mode(caplog):
    """An unrecognized header mode is dropped with a warning, never silently kept."""
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[
            {"name": "X-Keep", "mode": "literal", "value": "kept"},
            {"name": "X-Weird", "mode": "bogus", "value": "v"},
        ],
    )
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        [(_, dto)] = build_runtime_servers()
    assert dto.headers == {"X-Keep": "kept"}
    assert "unrecognized mode" in caplog.text


async def test_discover_tools_skips_builtin_without_network(monkeypatch):
    """Built-ins carry a placeholder ``builtin://`` URL; discovery must not attempt
    a (doomed) handshake against it."""
    from mcp_servers.models import MCPServer
    from mcp_servers.services import discover_tools

    called = {"n": 0}

    async def _should_not_run(payload):
        called["n"] += 1
        return {"ok": True, "tools": []}

    monkeypatch.setattr("mcp_servers.services.test_connection", _should_not_run)
    server = MCPServer(
        name="bi", source=MCPServer.Source.BUILTIN, transport=MCPServer.Transport.HTTP, url="builtin://bi"
    )
    assert await discover_tools(server) == []
    assert called["n"] == 0


@pytest.mark.django_db
def test_discover_tools_cached_degrades_on_decryption_error(monkeypatch):
    """The cache wrapper must swallow a DecryptionError so views never 500."""
    from django.core.cache import cache

    from mcp_servers.models import MCPServer
    from mcp_servers.services import discover_tools_cached

    from core.encryption import DecryptionError

    cache.clear()
    server = MCPServer.objects.create(name="rot", transport=MCPServer.Transport.HTTP, url="http://rot.test")

    async def _boom(srv):
        raise DecryptionError("key rotated")

    monkeypatch.setattr("mcp_servers.services.discover_tools", _boom)
    assert discover_tools_cached(server) == []


@pytest.mark.django_db
def test_discover_tools_cached_negative_caches_empty_with_short_ttl(monkeypatch):
    """An empty/unreachable result is cached with the short negative TTL (not the full
    success TTL): a broken server isn't re-probed every render, nor pinned empty for 60s."""
    from django.core.cache import cache

    from mcp_servers import services
    from mcp_servers.constants import TOOLS_CACHE_TIMEOUT, TOOLS_NEGATIVE_CACHE_TIMEOUT
    from mcp_servers.models import MCPServer

    cache.clear()
    server = MCPServer.objects.create(name="flap", transport=MCPServer.Transport.HTTP, url="http://flap.test")

    captured: dict = {}
    real_set = services.cache.set

    def spy_set(key, value, timeout=None, **kw):
        captured["timeout"] = timeout
        return real_set(key, value, timeout, **kw)

    async def _empty(srv):
        return []

    monkeypatch.setattr("mcp_servers.services.discover_tools", _empty)
    monkeypatch.setattr(services.cache, "set", spy_set)

    assert services.discover_tools_cached(server) == []
    assert captured["timeout"] == TOOLS_NEGATIVE_CACHE_TIMEOUT
    assert TOOLS_NEGATIVE_CACHE_TIMEOUT < TOOLS_CACHE_TIMEOUT


@pytest.mark.django_db
def test_discover_tools_cached_caches_success_with_full_ttl(monkeypatch):
    """A successful discovery is cached for the full TTL."""
    from django.core.cache import cache

    from mcp_servers import services
    from mcp_servers.constants import TOOLS_CACHE_TIMEOUT
    from mcp_servers.models import MCPServer

    cache.clear()
    server = MCPServer.objects.create(name="ok", transport=MCPServer.Transport.HTTP, url="http://ok.test")

    captured: dict = {}
    real_set = services.cache.set

    def spy_set(key, value, timeout=None, **kw):
        captured["timeout"] = timeout
        return real_set(key, value, timeout, **kw)

    async def _ok(srv):
        return [{"name": "t", "description": ""}]

    monkeypatch.setattr("mcp_servers.services.discover_tools", _ok)
    monkeypatch.setattr(services.cache, "set", spy_set)

    assert services.discover_tools_cached(server) == [{"name": "t", "description": ""}]
    assert captured["timeout"] == TOOLS_CACHE_TIMEOUT
