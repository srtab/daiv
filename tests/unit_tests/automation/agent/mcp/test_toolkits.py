import asyncio
from contextlib import AsyncExitStack

import anyio
import httpx2
import pytest

from automation.agent.mcp.client import MCPHTTPStatusError, build_client
from automation.agent.mcp.schemas import ToolFilter, UserMcpServer
from automation.agent.mcp.toolkits import MCPToolkit, _load_server_tools
from tests.unit_tests.automation.agent.mcp.helpers import serve

APP_TOOLS = {"echo", "write", "plain", "boom", "structured", "capabilities"}


def _dto(url: str, tool_filter: ToolFilter | None = None) -> UserMcpServer:
    return UserMcpServer(type="http", url=url, tool_filter=tool_filter)


async def _until_requested(gate) -> None:
    while not gate.request_headers:
        await asyncio.sleep(0.01)


@pytest.fixture
def gates(monkeypatch):
    """Route toolkit clients to in-process gates by URL; an unrouted URL fails fast (and is skipped)."""
    routes = {}

    def _build_client(transport, url, headers):
        return build_client(transport, url, headers, http_transport=httpx2.ASGITransport(app=routes[url]))

    monkeypatch.setattr("automation.agent.mcp.toolkits.build_client", _build_client)
    return routes


@pytest.fixture
def servers(monkeypatch):
    """Set the ``(name, dto)`` list ``MCPToolkit.get_tools`` resolves."""

    def _set(*entries):
        monkeypatch.setattr("mcp_servers.services.build_runtime_servers", lambda *args, **kwargs: list(entries))

    return _set


class TestGetTools:
    async def test_returns_empty_when_there_are_no_servers(self, servers):
        servers()

        assert await MCPToolkit.get_tools() == []

    async def test_prefixes_names_and_sets_tags_and_metadata(self, gates, servers):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp")))

            tools = await MCPToolkit.get_tools()

        assert {tool.name for tool in tools} == {f"acme_{name}" for name in APP_TOOLS}
        for tool in tools:
            assert tool.tags == ["mcp_server"]
            assert tool.metadata["mcp_server"] == "acme"
            assert "mcp" in tool.metadata

    async def test_renamed_tool_still_calls_the_original_mcp_tool(self, gates, servers):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp")))

            tools = await MCPToolkit.get_tools()
            echo = next(tool for tool in tools if tool.name == "acme_echo")
            result = await echo.ainvoke({"text": "hi"})

        assert result[0]["text"] == "hi"

    @pytest.mark.parametrize(
        ("tool_filter", "expected"),
        [
            (ToolFilter(mode="allow", items=["echo", "plain"]), {"echo", "plain"}),
            (ToolFilter(mode="block", items=["boom", "write", "structured", "capabilities"]), {"echo", "plain"}),
        ],
    )
    async def test_filter_applies_to_raw_mcp_names(self, gates, servers, tool_filter, expected):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp", tool_filter)))

            tools = await MCPToolkit.get_tools()

        assert {tool.name for tool in tools} == {f"acme_{name}" for name in expected}

    async def test_filter_that_removes_every_tool_contributes_nothing(self, gates, servers):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp", ToolFilter(mode="allow", items=["does-not-exist"]))))

            assert await MCPToolkit.get_tools() == []

    async def test_overlapping_server_names_do_not_cross_match_filters(self, gates, servers):
        async with AsyncExitStack() as stack:
            git_gate = await stack.enter_async_context(serve())
            hub_gate = await stack.enter_async_context(serve())
            gates["http://git/mcp"] = git_gate
            gates["http://git-hub/mcp"] = hub_gate
            servers(
                ("git", _dto("http://git/mcp", ToolFilter(mode="allow", items=["echo"]))),
                ("git_hub", _dto("http://git-hub/mcp")),
            )

            tools = await MCPToolkit.get_tools()

        names = {tool.name for tool in tools}
        assert {name for name in names if name.startswith("git_") and not name.startswith("git_hub_")} == {"git_echo"}
        assert {f"git_hub_{name}" for name in APP_TOOLS} <= names

    async def test_failing_server_does_not_blank_its_peers(self, gates, servers):
        async with AsyncExitStack() as stack:
            good = await stack.enter_async_context(serve())
            bad = await stack.enter_async_context(serve(status=503))
            gates["http://good/mcp"] = good
            gates["http://bad/mcp"] = bad
            servers(("bad", _dto("http://bad/mcp")), ("good", _dto("http://good/mcp")))

            tools = await MCPToolkit.get_tools()

        assert {tool.name for tool in tools} == {f"good_{name}" for name in APP_TOOLS}

    async def test_hanging_server_times_out_without_blanking_peers(self, gates, servers, monkeypatch, caplog):
        monkeypatch.setattr("automation.agent.mcp.toolkits.settings.TOOL_LOAD_TIMEOUT", 1.0)
        async with AsyncExitStack() as stack:
            good = await stack.enter_async_context(serve())
            slow = await stack.enter_async_context(serve(hang=True))
            gates["http://good/mcp"] = good
            gates["http://slow/mcp"] = slow
            servers(("slow", _dto("http://slow/mcp")), ("good", _dto("http://good/mcp")))

            with caplog.at_level("WARNING", logger="daiv.tools"):
                tools = await MCPToolkit.get_tools()

        assert {tool.name for tool in tools} == {f"good_{name}" for name in APP_TOOLS}
        [record] = [r for r in caplog.records if r.name == "daiv.tools"]
        assert record.levelname == "WARNING"
        assert record.exc_info is None
        message = record.getMessage()
        assert "'slow'" in message
        assert "http://slow/mcp" in message
        assert "timed out after 1s" in message


class TestLoadServerTools:
    async def test_outer_cancellation_propagates(self, gates):
        async with serve(hang=True) as gate:
            gates["http://slow/mcp"] = gate

            task = asyncio.create_task(_load_server_tools("slow", _dto("http://slow/mcp")))
            await asyncio.wait_for(_until_requested(gate), timeout=5)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task


class TestLogPolicy:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError(),
            anyio.BrokenResourceError(),
            anyio.ClosedResourceError(),
            MCPHTTPStatusError(503, "Service Unavailable", "http://x/mcp"),
        ],
        ids=["timeout", "broken-stream", "closed-stream", "http-5xx"],
    )
    async def test_soft_failures_warn_without_traceback(self, servers, monkeypatch, caplog, exc):
        async def _fail(client):
            raise exc

        monkeypatch.setattr("automation.agent.mcp.toolkits.list_tools", _fail)
        servers(("acme", _dto("http://acme/mcp")))

        with caplog.at_level("DEBUG", logger="daiv.tools"):
            assert await MCPToolkit.get_tools() == []

        records = [r for r in caplog.records if r.name == "daiv.tools" and r.levelname != "DEBUG"]
        assert [r.levelname for r in records] == ["WARNING"]
        assert records[0].exc_info is None
        assert "acme" in records[0].getMessage()

    @pytest.mark.parametrize(
        "exc",
        [
            MCPHTTPStatusError(401, "Unauthorized", "http://x/mcp"),
            ValueError("bug"),
            httpx2.ConnectError("refused"),
            ExceptionGroup("g", [MCPHTTPStatusError(503, "Service Unavailable", "http://x/mcp"), ValueError("bug")]),
        ],
        ids=["http-4xx", "unexpected", "unreachable", "mixed-group"],
    )
    async def test_other_failures_log_with_traceback(self, servers, monkeypatch, caplog, exc):
        async def _fail(client):
            raise exc

        monkeypatch.setattr("automation.agent.mcp.toolkits.list_tools", _fail)
        servers(("acme", _dto("http://acme/mcp")))

        with caplog.at_level("DEBUG", logger="daiv.tools"):
            assert await MCPToolkit.get_tools() == []

        records = [r for r in caplog.records if r.name == "daiv.tools" and r.levelname != "DEBUG"]
        assert [r.levelname for r in records] == ["ERROR"]
        assert records[0].exc_info is not None

    async def test_upstream_503_through_the_real_client_stack_is_a_warning(self, gates, servers, caplog):
        async with serve(status=503) as gate:
            gates["http://bad/mcp"] = gate
            servers(("bad", _dto("http://bad/mcp")))

            with caplog.at_level("WARNING", logger="daiv.tools"):
                assert await MCPToolkit.get_tools() == []

        assert [r.levelname for r in caplog.records if r.name == "daiv.tools"] == ["WARNING"]


@pytest.mark.django_db(transaction=True)
async def test_end_to_end_db_row_yields_prefixed_tools(gates):
    from asgiref.sync import sync_to_async
    from mcp_servers.models import MCPServer

    await sync_to_async(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete)()
    await sync_to_async(MCPServer.objects.create)(
        name="acme", transport=MCPServer.Transport.HTTP, url="http://acme.test/mcp"
    )

    async with serve() as gate:
        gates["http://acme.test/mcp"] = gate
        tools = await MCPToolkit.get_tools()

    assert {f"acme_{name}" for name in APP_TOOLS} <= {tool.name for tool in tools}


@pytest.mark.django_db(transaction=True)
async def test_end_to_end_db_tool_filter_is_applied(gates):
    from asgiref.sync import sync_to_async
    from mcp_servers.models import MCPServer

    await sync_to_async(MCPServer.objects.filter(source=MCPServer.Source.BUILTIN).delete)()
    await sync_to_async(MCPServer.objects.create)(
        name="acme",
        transport=MCPServer.Transport.HTTP,
        url="http://acme.test/mcp",
        tool_filter_mode=MCPServer.FilterMode.BLOCK,
        tool_filter_items=["boom"],
    )

    async with serve() as gate:
        gates["http://acme.test/mcp"] = gate
        tools = await MCPToolkit.get_tools()

    names = {tool.name for tool in tools if tool.name.startswith("acme_")}
    assert "acme_echo" in names
    assert "acme_boom" not in names
