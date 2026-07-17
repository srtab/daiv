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
def test_returns_only_enabled_rows():
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
    MCPServer.objects.create(name="on", transport=MCPServer.Transport.HTTP, url="http://on", enabled=True)
    MCPServer.objects.create(name="off", transport=MCPServer.Transport.HTTP, url="http://off", enabled=False)
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
        enabled=True,
    )
    out = build_runtime_servers()
    assert "sentry-x" in [name for name, _ in out]


@pytest.mark.django_db
def test_disabled_builtin_row_excluded():
    # Same context7-leak caveat as the exact-output tests above: this test's own assertion
    # (out == []) only holds once the seeded enabled built-ins are cleared.
    MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete()
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


async def test_test_connection_returns_tools_on_success(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from mcp_servers.services import test_connection

    fake_client = MagicMock()
    ro_tool = MagicMock(name="demo_tool", description="demo", metadata={"readOnlyHint": True})
    ro_tool.name = "demo_tool"  # `name` is a MagicMock ctor kwarg, so set it explicitly
    rw_tool = MagicMock(description="mutates", metadata={"readOnlyHint": False})
    rw_tool.name = "write_tool"
    plain_tool = MagicMock(description="", metadata=None)  # server left it unannotated
    plain_tool.name = "plain_tool"
    fake_client.get_tools = AsyncMock(return_value=[ro_tool, rw_tool, plain_tool])

    monkeypatch.setattr("mcp_servers.services._build_client", lambda payload: fake_client)

    result = await test_connection({"transport": "http", "url": "http://demo.test/mcp", "headers": []})
    assert result["ok"] is True
    assert [t["name"] for t in result["tools"]] == ["demo_tool", "write_tool", "plain_tool"]
    # readOnlyHint is surfaced tri-state: True / False / None (unannotated).
    assert [t["read_only"] for t in result["tools"]] == [True, False, None]


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
    """The MCP streamable-http client runs inside an anyio task group, so a real
    failure (e.g. an httpx 401) surfaces wrapped in an ExceptionGroup whose str()
    is the useless "unhandled errors in a TaskGroup (N sub-exceptions)". The error
    must unwrap to the underlying cause, not the wrapper."""
    from mcp_servers.services import test_connection

    def _fail(payload):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("401 Unauthorized")])

    monkeypatch.setattr("mcp_servers.services._build_client", _fail)
    result = await test_connection({"transport": "http", "url": "http://x.test", "headers": []})
    assert result["ok"] is False
    assert "401 Unauthorized" in result["error"]
    assert "RuntimeError" in result["error"]
    # The opaque wrapper must NOT be what the user sees.
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


def test_format_error_trims_httpx_noise_line():
    """httpx exceptions append a "For more information check: <url>" line that is
    noise in the UI — only the first line of the underlying cause is kept."""
    import httpx
    from mcp_servers.services import _format_error

    real = httpx.HTTPStatusError(
        "Client error '401 Unauthorized' for url 'https://mcp.sentry.dev/mcp'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401",
        request=httpx.Request("POST", "https://mcp.sentry.dev/mcp"),
        response=httpx.Response(401),
    )
    message = _format_error(ExceptionGroup("unhandled errors in a TaskGroup", [real]))
    assert message == "HTTPStatusError: Client error '401 Unauthorized' for url 'https://mcp.sentry.dev/mcp'"
    assert "For more information check" not in message


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
        enabled=True,
    )
    MCPServer.objects.create(
        name="mine",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="https://u.test/mcp",
        enabled=True,
    )

    names_anon = [n for n, _ in services.build_runtime_servers()]
    assert names_anon == ["glob"]  # no user → globals only

    names_user = [n for n, _ in services.build_runtime_servers(user_id=member_user.id)]
    assert set(names_user) == {"glob", "mine"}


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
        enabled=True,
    )
    MCPServer.objects.create(
        name="dup",
        scope=MCPServer.Scope.USER,
        user=member_user,
        transport=MCPServer.Transport.HTTP,
        url="https://user.test/mcp",
        enabled=True,
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
        enabled=True,
        headers=[
            {"name": "X-Lit", "mode": "literal", "value": "ok"},
            {"name": "X-Env", "mode": "env_ref", "value": "SOME_HOST_VAR"},
        ],
    )
    result = dict(services.build_runtime_servers(user_id=member_user.id))
    assert result["mine"].headers == {"X-Lit": "ok"}  # env_ref dropped


async def test_mcptoolkit_forwards_user_id(monkeypatch):
    from automation.agent.mcp import toolkits

    seen = {}

    def fake_build(user_id=None):
        seen["user_id"] = user_id
        return []

    monkeypatch.setattr("mcp_servers.services.build_runtime_servers", fake_build)
    tools = await toolkits.MCPToolkit.get_tools(user_id=42)
    assert tools == []
    assert seen["user_id"] == 42


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
