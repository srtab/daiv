from __future__ import annotations

import pytest
from mcp_servers import services
from mcp_servers.models import MCPServer
from mcp_servers.services import build_runtime_servers

from automation.agent.mcp.schemas import UserMcpServer


@pytest.fixture(autouse=True)
def _no_network_tool_sync():
    """Override the directory-wide autouse stub (``tests/unit_tests/mcp_servers/conftest.py``)
    for this module: it patches ``services.sync_discovered_tools`` itself, which this file
    exercises directly (mocking only the lower-level ``services.test_connection``)."""
    yield


@pytest.mark.django_db
def test_returns_only_active_rows():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="on", transport=MCPServer.Transport.HTTP, url="http://on", status=MCPServer.Status.ACTIVE
    )
    MCPServer.objects.create(
        name="off", transport=MCPServer.Transport.HTTP, url="http://off", status=MCPServer.Status.DISABLED
    )
    out = build_runtime_servers()
    names = [dto_name for dto_name, _ in out]
    assert names == ["on"]


@pytest.mark.django_db
def test_literal_headers_decrypt_into_dto():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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
def test_malformed_row_skipped_keeps_others(caplog):
    """A row whose persisted transport is outside the DTO's allowed literals (reachable only via a
    raw DB write, since the form and model choices otherwise constrain it) must be skipped without
    blanking tools from healthy peers — matching the per-server isolation MCPToolkit.get_tools relies on."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    bad = MCPServer.objects.create(name="bad", transport=MCPServer.Transport.HTTP, url="http://bad")
    MCPServer.objects.create(name="good", transport=MCPServer.Transport.HTTP, url="http://good")
    # Bypass form/model choice validation to persist an invalid transport.
    MCPServer.objects.filter(pk=bad.pk).update(transport="websocket")

    with caplog.at_level("ERROR", logger="daiv.mcp_servers"):
        out = dict(build_runtime_servers())

    assert "bad" not in out
    assert "good" in out
    assert "could not be converted to a runtime DTO" in caplog.text


@pytest.mark.django_db
def test_builtin_row_included_in_runtime_servers():
    MCPServer.objects.create(
        name="sentry-x",
        source=MCPServer.Source.BUILTIN,
        transport=MCPServer.Transport.HTTP,
        url="https://mcp.sentry.dev/mcp",
        status=MCPServer.Status.ACTIVE,
    )
    out = build_runtime_servers()
    assert "sentry-x" in [name for name, _ in out]


@pytest.mark.django_db
def test_disabled_builtin_row_excluded():
    # Same context7-leak caveat as the exact-output tests above: this test's own assertion
    # (out == []) only holds once the seeded active built-ins are cleared.
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="fake-builtin",
        source=MCPServer.Source.BUILTIN,
        transport=MCPServer.Transport.HTTP,
        url="http://db",
        status=MCPServer.Status.DISABLED,
    )
    out = build_runtime_servers()
    assert out == []


@pytest.mark.django_db
def test_tool_filter_round_trips_through_dto():
    from automation.agent.mcp.schemas import ToolFilter

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="srv2",
        transport=MCPServer.Transport.HTTP,
        url="http://srv2.test",
        tool_filter_mode=MCPServer.FilterMode.NONE,
        tool_filter_items=["foo"],  # ignored when mode is NONE
    )
    [(_, dto)] = build_runtime_servers()
    assert dto.tool_filter is None


async def test_test_connection_returns_tools_with_tristate_read_only(monkeypatch):
    from mcp_servers.services import test_connection

    from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve

    async with serve() as gate:
        monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: client_for(gate))
        result = await test_connection({"transport": "http", "url": "http://test/mcp", "headers": []})

    assert result["ok"] is True
    read_only = {tool["name"]: tool["read_only"] for tool in result["tools"]}
    assert (read_only["echo"], read_only["write"], read_only["plain"]) == (True, False, None)
    assert next(tool for tool in result["tools"] if tool["name"] == "echo")["description"] == "Echo the text back."


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


async def test_test_connection_unwraps_exception_group(monkeypatch):
    """An ExceptionGroup's str() is the useless "unhandled errors in a TaskGroup"; the error must name the leaf."""
    from mcp_servers.services import test_connection

    def _fail(payload):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("401 Unauthorized")])

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})
    assert result["ok"] is False
    assert "401 Unauthorized" in result["error"]
    assert "RuntimeError" in result["error"]
    assert "unhandled errors in a TaskGroup" not in result["error"]
    assert "ExceptionGroup" not in result["error"]


async def test_test_connection_unwraps_nested_exception_groups(monkeypatch):
    """Groups can nest (a group inside a group); flattening must reach the leaves."""
    from mcp_servers.services import test_connection

    def _fail(payload):
        inner = ExceptionGroup("inner", [ValueError("bad url")])
        raise ExceptionGroup("outer", [inner])

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})
    assert result["ok"] is False
    assert "bad url" in result["error"]
    assert "ValueError" in result["error"]
    assert "unhandled errors in a TaskGroup" not in result["error"]


def _service_records(caplog):
    return [r for r in caplog.records if r.name == "daiv.mcp_servers"]


@pytest.mark.parametrize(("status", "reason"), [(401, "Unauthorized"), (503, "Service Unavailable")])
async def test_test_connection_reports_http_status(monkeypatch, caplog, status, reason):
    from mcp_servers.services import test_connection

    from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve

    async with serve(status=status) as gate:
        monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: client_for(gate))
        with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
            result = await test_connection({"transport": "http", "url": "http://test/mcp", "headers": []})

    assert result == {"ok": False, "error": f"HTTP {status} {reason} for url 'http://test/mcp'"}
    assert [r.levelname for r in _service_records(caplog)] == ["WARNING"]


async def test_test_connection_reports_unreachable_host_as_warning(caplog):
    from mcp_servers.services import test_connection

    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        result = await test_connection({"transport": "http", "url": "http://127.0.0.1:1/mcp", "headers": []})

    assert result["ok"] is False
    assert result["error"].startswith("ConnectError:")
    assert [r.levelname for r in _service_records(caplog)] == ["WARNING"]


async def test_test_connection_warns_on_a_group_of_anticipated_failures(monkeypatch, caplog):
    import anyio
    import httpx2
    from mcp_servers.services import test_connection

    def _fail(payload):
        raise ExceptionGroup("g", [httpx2.ConnectError("refused"), anyio.ClosedResourceError()])

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})

    assert result == {"ok": False, "error": "ConnectError: refused; ClosedResourceError"}
    [record] = _service_records(caplog)
    assert record.levelname == "WARNING"


async def test_test_connection_logs_unexpected_failures_with_traceback(monkeypatch, caplog):
    from mcp_servers.services import test_connection

    def _fail(payload):
        raise ValueError("bug")

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})

    assert result == {"ok": False, "error": "ValueError: bug"}
    records = _service_records(caplog)
    assert [r.levelname for r in records] == ["ERROR"]
    assert records[0].exc_info is not None


async def test_test_connection_survives_a_200_response_that_is_not_mcp(monkeypatch):
    from mcp_servers.services import test_connection

    from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve

    async with serve(status=200, body=b"<html>not mcp</html>", content_type=b"text/html") as gate:
        monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: client_for(gate))
        result = await test_connection({"transport": "http", "url": "http://test/mcp", "headers": []})

    assert result["ok"] is False
    assert result["error"]


async def test_test_connection_times_out_on_a_hanging_server(monkeypatch):
    from mcp_servers.services import test_connection

    from tests.unit_tests.automation.agent.mcp.helpers import client_for, serve

    monkeypatch.setattr("mcp_servers.services._TEST_CONNECTION_TIMEOUT", 0.3)
    async with serve(hang=True) as gate:
        monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: client_for(gate))
        result = await test_connection({"transport": "http", "url": "http://test/mcp", "headers": []})

    assert result == {"ok": False, "error": "Connection timed out after 0.3s"}


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
def test_server_health_flags_unexpanded_env_ref_in_literal():
    """A legacy header imported as a literal that still contains a ``${...}`` reference
    (migration 0002 can't split ``"Bearer ${TOKEN}"``) will never expand at runtime.
    ``server_health`` must flag it rather than reporting ok=True — otherwise the list-view
    badge lies while the agent silently runs without that server's auth header."""
    from mcp_servers.models import MCPServer
    from mcp_servers.services import server_health

    s = MCPServer.objects.create(
        name="legacy-lit",
        transport=MCPServer.Transport.HTTP,
        url="http://legacy.test",
        headers=[{"name": "Authorization", "mode": "literal", "value": "Bearer ${SENTRY_TOKEN}"}],
    )
    health = server_health(s)
    assert health["ok"] is False
    assert "Authorization" in health["reason"]


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
    from fastmcp.client.transports import StreamableHttpTransport
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

    assert isinstance(client.transport, StreamableHttpTransport)
    assert client.transport.url == "http://demo.test/mcp"
    assert client.transport.headers == {"X-Lit": "lit", "X-Env": "from-env"}


def test_build_client_maps_sse_transport():
    from fastmcp.client.transports import SSETransport
    from mcp_servers.services import _build_client

    client = _build_client({"transport": "sse", "url": "http://demo.test/sse", "headers": []})

    assert isinstance(client.transport, SSETransport)
    assert client.transport.url == "http://demo.test/sse"
    assert client.transport.headers == {}


def test_build_client_rejects_unknown_transport():
    from mcp_servers.services import _build_client

    with pytest.raises(ValueError, match="Unsupported transport"):
        _build_client({"transport": "carrier-pigeon", "url": "http://x.test", "headers": []})


def test_build_client_warns_on_missing_env_ref(caplog, monkeypatch):
    from mcp_servers.services import _build_client

    monkeypatch.delenv("ABSENT", raising=False)
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        client = _build_client({
            "transport": "http",
            "url": "http://x.test",
            "headers": [{"name": "X-Env", "mode": "env_ref", "value": "ABSENT"}],
        })

    assert client.transport.headers == {}
    assert "ABSENT" in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("bad_header", "expected_log"),
    [
        ({"name": "X-Weird", "mode": "bogus", "value": "v"}, "unrecognized mode"),
        ({"name": "", "mode": "literal", "value": "orphan"}, "no name"),
    ],
)
def test_build_runtime_servers_drops_bad_header_with_warning(bad_header, expected_log, caplog):
    """A malformed header — an unrecognized mode, or no name (both reachable only via a raw DB
    write) — is dropped loudly, never silently kept."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="srv",
        transport=MCPServer.Transport.HTTP,
        url="http://srv",
        headers=[{"name": "X-Keep", "mode": "literal", "value": "kept"}, bad_header],
    )
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        [(_, dto)] = build_runtime_servers()
    assert dto.headers == {"X-Keep": "kept"}
    assert expected_log in caplog.text


@pytest.mark.django_db
def test_exposed_tools_none_returns_all():
    s = MCPServer.objects.create(
        name="ex-none",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        tool_filter_mode=MCPServer.FilterMode.NONE,
        discovered_tools=[
            {"name": "a", "description": "", "read_only": None},
            {"name": "b", "description": "", "read_only": True},
        ],
    )
    assert [t["name"] for t in services.exposed_tools(s)] == ["a", "b"]


@pytest.mark.django_db
def test_exposed_tools_allow_keeps_only_listed():
    s = MCPServer.objects.create(
        name="ex-allow",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        tool_filter_mode=MCPServer.FilterMode.ALLOW,
        tool_filter_items=["a"],
        discovered_tools=[{"name": "a", "description": ""}, {"name": "b", "description": ""}],
    )
    assert [t["name"] for t in services.exposed_tools(s)] == ["a"]


@pytest.mark.django_db
def test_exposed_tools_block_drops_listed():
    s = MCPServer.objects.create(
        name="ex-block",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        tool_filter_mode=MCPServer.FilterMode.BLOCK,
        tool_filter_items=["a"],
        discovered_tools=[{"name": "a", "description": ""}, {"name": "b", "description": ""}],
    )
    assert [t["name"] for t in services.exposed_tools(s)] == ["b"]


@pytest.mark.django_db
def test_sync_discovered_tools_ok_persists_snapshot(monkeypatch):
    s = MCPServer.objects.create(name="sync-ok", transport=MCPServer.Transport.HTTP, url="http://x")

    async def fake_test_connection(payload):
        return {"ok": True, "tools": [{"name": "t", "description": "d", "read_only": True}]}

    monkeypatch.setattr(services, "test_connection", fake_test_connection)
    result = services.sync_discovered_tools(s)
    s.refresh_from_db()
    assert result == {"ok": True, "count": 1}
    assert s.discovered_tools == [{"name": "t", "description": "d", "read_only": True}]
    assert s.tools_synced_at is not None


@pytest.mark.django_db
def test_sync_discovered_tools_failure_preserves_prior_snapshot(monkeypatch):
    s = MCPServer.objects.create(
        name="sync-fail",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        discovered_tools=[{"name": "old", "description": ""}],
    )

    async def fake_test_connection(payload):
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr(services, "test_connection", fake_test_connection)
    result = services.sync_discovered_tools(s)
    s.refresh_from_db()
    assert result["ok"] is False
    assert s.discovered_tools == [{"name": "old", "description": ""}]  # untouched
    assert s.tools_synced_at is None  # not stamped on failure


@pytest.mark.django_db
def test_sync_discovered_tools_ok_empty_clears_prior_snapshot(monkeypatch):
    """A server that genuinely exposes zero tools (``ok=True, tools=[]``) must be *recorded*
    as synced — clearing a stale snapshot and stamping the timestamp. This is the axis that
    distinguishes 'recorded zero' from 'preserved on failure'; conflating empty-with-failure
    would leave the admin UI showing a stale catalog forever."""
    s = MCPServer.objects.create(
        name="sync-empty",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        discovered_tools=[{"name": "old", "description": ""}],
    )

    async def fake_test_connection(payload):
        return {"ok": True, "tools": []}

    monkeypatch.setattr(services, "test_connection", fake_test_connection)
    result = services.sync_discovered_tools(s)
    s.refresh_from_db()
    assert result == {"ok": True, "count": 0}
    assert s.discovered_tools == []  # stale snapshot cleared, not preserved
    assert s.tools_synced_at is not None  # genuinely-empty sync is still a sync


@pytest.mark.django_db
def test_build_runtime_servers_merges_user_and_global(member_user):
    from mcp_servers import services
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="glob",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="https://g.test/mcp",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="mine",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="https://u.test/mcp",
        status=MCPServer.Status.ACTIVE,
    )

    names_anon = [n for n, _ in services.build_runtime_servers()]
    assert names_anon == ["glob"]  # no user → globals only

    names_user = [n for n, _ in services.build_runtime_servers(user_id=member_user.id)]
    assert names_user == ["glob", "mine"]  # globals first, then user rows, each name-sorted


@pytest.mark.django_db
def test_build_runtime_servers_global_wins_on_name_collision(member_user):
    from mcp_servers import services
    from mcp_servers.models import MCPServer

    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="dup",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="https://global.test/mcp",
        status=MCPServer.Status.ACTIVE,
    )
    MCPServer.objects.create(
        name="dup",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="https://user.test/mcp",
        status=MCPServer.Status.ACTIVE,
    )

    result = dict(services.build_runtime_servers(user_id=member_user.id))
    assert list(result) == ["dup"]
    assert result["dup"].url == "https://global.test/mcp"  # global row wins


@pytest.mark.django_db
def test_build_runtime_servers_strips_env_ref_on_user_rows(member_user):
    from mcp_servers import services
    from mcp_servers.models import MCPServer

    MCPServer.objects.create(
        name="mine",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="https://u.test/mcp",
        status=MCPServer.Status.ACTIVE,
        headers=[
            {"name": "X-Lit", "mode": "literal", "value": "ok"},
            {"name": "X-Env", "mode": "env_ref", "value": "SOME_HOST_VAR"},
        ],
    )
    result = dict(services.build_runtime_servers(user_id=member_user.id))
    assert result["mine"].headers == {"X-Lit": "ok"}  # env_ref dropped


async def test_mcptoolkit_forwards_user_id_and_overrides(monkeypatch):
    from automation.agent.mcp import toolkits

    seen = {}

    def fake_build(user_id=None, overrides=None):
        seen["user_id"] = user_id
        seen["overrides"] = overrides
        return []

    monkeypatch.setattr("mcp_servers.services.build_runtime_servers", fake_build)
    tools = await toolkits.MCPToolkit.get_tools(user_id=42, overrides={"a": "off"})
    assert tools == []
    assert seen == {"user_id": 42, "overrides": {"a": "off"}}


@pytest.mark.django_db
def test_sync_discovered_tools_decryption_error_preserves_snapshot(monkeypatch):
    """If a server's headers can't be decrypted (e.g. key rotation), sync must return an
    error without probing the network or touching the known-good snapshot — never a 500."""
    s = MCPServer.objects.create(
        name="sync-dec",
        transport=MCPServer.Transport.HTTP,
        url="http://x",
        headers=[{"name": "X", "mode": "literal", "value": "secret"}],
        discovered_tools=[{"name": "old", "description": ""}],
    )
    MCPServer.objects.filter(pk=s.pk).update(_headers_encrypted="not-a-fernet-token")
    s.refresh_from_db()

    probed = False

    async def fake_test_connection(payload):
        nonlocal probed
        probed = True
        return {"ok": True, "tools": []}

    monkeypatch.setattr(services, "test_connection", fake_test_connection)
    result = services.sync_discovered_tools(s)
    s.refresh_from_db()
    assert result == {"ok": False, "error": "headers cannot be decrypted"}
    assert probed is False  # never reached the network probe
    assert s.discovered_tools == [{"name": "old", "description": ""}]  # untouched
    assert s.tools_synced_at is None


@pytest.mark.django_db
def test_default_set_is_active_only():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a", transport=MCPServer.Transport.HTTP, url="http://a", status=MCPServer.Status.ACTIVE
    )
    MCPServer.objects.create(
        name="b", transport=MCPServer.Transport.HTTP, url="http://b", status=MCPServer.Status.ON_DEMAND
    )
    MCPServer.objects.create(
        name="c", transport=MCPServer.Transport.HTTP, url="http://c", status=MCPServer.Status.DISABLED
    )
    assert [n for n, _ in build_runtime_servers()] == ["a"]


@pytest.mark.django_db
def test_override_off_drops_a_default():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a", transport=MCPServer.Transport.HTTP, url="http://a", status=MCPServer.Status.ACTIVE
    )
    assert [n for n, _ in build_runtime_servers(overrides={"a": "off"})] == []


@pytest.mark.django_db
def test_override_on_adds_an_on_demand():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="b", transport=MCPServer.Transport.HTTP, url="http://b", status=MCPServer.Status.ON_DEMAND
    )
    assert [n for n, _ in build_runtime_servers()] == []
    assert [n for n, _ in build_runtime_servers(overrides={"b": "on"})] == ["b"]


@pytest.mark.django_db
def test_override_on_for_disabled_is_ignored():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="d", transport=MCPServer.Transport.HTTP, url="http://d", status=MCPServer.Status.DISABLED
    )
    assert [n for n, _ in build_runtime_servers(overrides={"d": "on"})] == []


@pytest.mark.django_db
def test_override_on_for_unknown_and_off_for_absent_are_noops():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a", transport=MCPServer.Transport.HTTP, url="http://a", status=MCPServer.Status.ACTIVE
    )
    assert [n for n, _ in build_runtime_servers(overrides={"ghost": "on", "also-absent": "off"})] == ["a"]


@pytest.mark.django_db
def test_malformed_override_value_ignored_and_logged(caplog):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="a", transport=MCPServer.Transport.HTTP, url="http://a", status=MCPServer.Status.ACTIVE
    )
    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        names = [n for n, _ in build_runtime_servers(overrides={"a": True, "x": "banana"})]
    assert names == ["a"]  # "a": True is not "off" → left at its default (on); "x" ignored
    assert caplog.text.count("unrecognized value") == 2  # both malformed values logged


@pytest.mark.django_db
def test_on_demand_global_shadows_active_user_row(member_user):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="dup",
        scope=MCPServer.Scope.GLOBAL,
        transport=MCPServer.Transport.HTTP,
        url="http://global",
        status=MCPServer.Status.ON_DEMAND,
    )
    MCPServer.objects.create(
        name="dup",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="http://user",
        status=MCPServer.Status.ACTIVE,
    )
    # Default load: neither (global is on-demand, user is shadowed out).
    assert [n for n, _ in build_runtime_servers(user_id=member_user.id)] == []
    # "on" resolves to the GLOBAL row, not the user row.
    result = dict(build_runtime_servers(user_id=member_user.id, overrides={"dup": "on"}))
    assert result["dup"].url == "http://global"


@pytest.mark.django_db
def test_on_demand_user_server_resolves_only_for_owner(member_user, admin_user):
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="mine",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="http://mine",
        status=MCPServer.Status.ON_DEMAND,
    )
    assert [n for n, _ in build_runtime_servers(user_id=member_user.id, overrides={"mine": "on"})] == ["mine"]
    assert [n for n, _ in build_runtime_servers(user_id=admin_user.id, overrides={"mine": "on"})] == []


# ---------------------------------------------------------------------------
# Composer Tools group (chat options sheet)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_composer_rows_report_the_tool_filter_effect():
    """The row shows what ``tool_filter_mode`` already does; it does not offer a second
    per-tool control."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="filtered",
        transport=MCPServer.Transport.HTTP,
        url="http://f",
        discovered_tools=[{"name": "a"}, {"name": "b"}, {"name": "c"}],
        tool_filter_mode=MCPServer.FilterMode.ALLOW,
        tool_filter_items=["a"],
    )

    [row] = services.composer_server_rows(services.deduped_pool_rows(None))
    assert row["name"] == "filtered"
    assert row["scope"] == "global"
    assert row["tools"] == 3
    assert row["exposed"] == 1
    assert row["filtered"] is True
    assert row["available"] is True
    assert row["is_default"] is True


@pytest.mark.django_db
def test_composer_rows_list_on_demand_servers_as_non_default():
    """An on-demand server the sheet never showed is a server nobody could opt into."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(
        name="opt-in", transport=MCPServer.Transport.HTTP, url="http://o", status=MCPServer.Status.ON_DEMAND
    )
    MCPServer.objects.create(
        name="off", transport=MCPServer.Transport.HTTP, url="http://x", status=MCPServer.Status.DISABLED
    )

    rows = services.composer_server_rows(services.deduped_pool_rows(None))
    assert [(row["name"], row["is_default"]) for row in rows] == [("opt-in", False)]


@pytest.mark.django_db
def test_composer_rows_flag_a_server_that_cannot_resolve_its_headers(monkeypatch):
    """``_load_server_tools`` degrades such a server to zero tools, so the composer greys
    it out instead of offering a switch that silently does nothing. Only the fact travels:
    the health reason names a global server's missing env vars, and this payload is read by
    every member who opens a session page — the server list that shows it is admin-gated."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    MCPServer.objects.create(
        name="broken",
        description="internal ops server",
        transport=MCPServer.Transport.HTTP,
        url="http://b",
        headers=[{"name": "X-Token", "mode": "env_ref", "value": "ABSENT_TOKEN"}],
    )

    [row] = services.composer_server_rows(services.deduped_pool_rows(None))
    assert row["available"] is False
    assert "ABSENT_TOKEN" not in str(row)
    assert "internal ops server" not in str(row)


@pytest.mark.django_db
def test_shadow_warning_is_runtime_only(caplog, member_user):
    """``deduped_pool_rows`` now feeds the composer sheet and both form pickers as well as
    the runtime, so warning unconditionally would say nothing new once per navigation and
    drown the log it is meant to stand out in."""
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(name="dup", transport=MCPServer.Transport.HTTP, url="http://g")
    MCPServer.objects.create(
        name="dup", scope=MCPServer.Scope.USER, user=member_user, transport=MCPServer.Transport.HTTP, url="http://u"
    )

    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        services.deduped_pool_rows(member_user.pk)
    assert "shadowed by a non-disabled global" not in caplog.text

    with caplog.at_level("WARNING", logger="daiv.mcp_servers"):
        build_runtime_servers(member_user.pk)
    assert "shadowed by a non-disabled global" in caplog.text
