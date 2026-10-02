import asyncio
from contextlib import AsyncExitStack

import anyio
import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from automation.agent.mcp.schemas import ToolFilter, UserMcpServer
from automation.agent.mcp.toolkits import MCPToolkit, _load_server_tools
from tests.unit_tests.automation.agent.mcp.helpers import APP_TOOLS, client_for, empty_event_stream, serve, status_error


def _dto(url: str, tool_filter: ToolFilter | None = None, headers: dict[str, str] | None = None) -> UserMcpServer:
    return UserMcpServer(type="http", url=url, tool_filter=tool_filter, headers=headers)


async def _until_requested(gate) -> None:
    while not gate.request_headers:
        await asyncio.sleep(0.01)


@pytest.fixture
def gates(monkeypatch):
    """Route toolkit clients to in-process gates by URL; an unrouted URL fails fast (and is skipped)."""
    routes = {}

    def _build_client(transport, url, headers):
        return client_for(routes[url], transport=transport, url=url, headers=headers)

    monkeypatch.setattr("automation.agent.mcp.toolkits.build_client", _build_client)
    return routes


@pytest.fixture
def servers(monkeypatch):
    """Set the ``(name, dto)`` list ``MCPToolkit.get_tools`` resolves."""

    def _set(*entries):
        monkeypatch.setattr("mcp_connectors.services.build_runtime_servers", lambda *args, **kwargs: list(entries))

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

    async def test_server_headers_reach_the_server(self, gates, servers):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp", headers={"Authorization": "Bearer t0k"})))

            await MCPToolkit.get_tools()

        assert gate.request_headers
        assert all(h.get("authorization") == "Bearer t0k" for h in gate.request_headers)

    async def test_failing_tool_returns_an_error_tool_message(self, gates, servers):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp")))

            tools = await MCPToolkit.get_tools()
            boom = next(tool for tool in tools if tool.name == "acme_boom")
            message = await boom.ainvoke({"id": "call-1", "name": "acme_boom", "args": {}, "type": "tool_call"})

        assert message.status == "error"
        assert message.name == "acme_boom"

    @pytest.mark.parametrize(
        ("tool_filter", "expected"),
        [
            (ToolFilter(mode="allow", items=["echo", "plain"]), {"echo", "plain"}),
            (ToolFilter(mode="block", items=["boom", "write", "capabilities"]), {"echo", "plain"}),
        ],
    )
    async def test_filter_applies_to_raw_mcp_names(self, gates, servers, tool_filter, expected):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp", tool_filter)))

            tools = await MCPToolkit.get_tools()

        assert {tool.name for tool in tools} == {f"acme_{name}" for name in expected}

    async def test_filter_that_removes_every_tool_contributes_nothing(self, gates, servers, caplog):
        async with serve() as gate:
            gates["http://acme/mcp"] = gate
            servers(("acme", _dto("http://acme/mcp", ToolFilter(mode="allow", items=["does-not-exist"]))))

            with caplog.at_level("WARNING", logger="daiv.tools"):
                assert await MCPToolkit.get_tools() == []

        assert gate.request_headers
        assert [r for r in caplog.records if r.name == "daiv.tools"] == []

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
        ("exc", "level"),
        [
            pytest.param(TimeoutError(), "WARNING", id="timeout"),
            pytest.param(anyio.BrokenResourceError(), "WARNING", id="broken-stream"),
            pytest.param(anyio.ClosedResourceError(), "WARNING", id="closed-stream"),
            pytest.param(status_error(503), "WARNING", id="http-5xx"),
            pytest.param(MCPError(CONNECTION_CLOSED, "Connection closed"), "WARNING", id="connection-closed"),
            pytest.param(
                ExceptionGroup("g", [status_error(503), anyio.BrokenResourceError()]), "WARNING", id="all-soft-group"
            ),
            pytest.param(status_error(401), "ERROR", id="http-4xx"),
            pytest.param(ValueError("bug"), "ERROR", id="unexpected"),
            pytest.param(httpx2.ConnectError("refused"), "ERROR", id="unreachable"),
            pytest.param(ExceptionGroup("g", [status_error(503), ValueError("bug")]), "ERROR", id="mixed-group"),
        ],
    )
    async def test_soft_failures_warn_and_others_log_a_traceback(self, servers, monkeypatch, caplog, exc, level):
        async def _fail(client):
            raise exc

        monkeypatch.setattr("automation.agent.mcp.toolkits.list_tools", _fail)
        servers(("acme", _dto("http://acme/mcp")))

        with caplog.at_level("DEBUG", logger="daiv.tools"):
            assert await MCPToolkit.get_tools() == []

        [record] = [r for r in caplog.records if r.name == "daiv.tools" and r.levelname != "DEBUG"]
        assert record.levelname == level
        assert (record.exc_info is not None) == (level == "ERROR")
        assert "acme" in record.getMessage()

    async def test_upstream_503_through_the_real_client_stack_is_a_warning(self, gates, servers, caplog):
        async with serve(status=503) as gate:
            gates["http://bad/mcp"] = gate
            servers(("bad", _dto("http://bad/mcp")))

            with caplog.at_level("WARNING", logger="daiv.tools"):
                assert await MCPToolkit.get_tools() == []

        assert [r.levelname for r in caplog.records if r.name == "daiv.tools"] == ["WARNING"]

    async def test_stream_dropped_mid_request_through_the_real_client_stack_is_a_warning(self, gates, servers, caplog):
        async with serve(routes={"tools/list": empty_event_stream()}) as gate:
            gates["http://bad/mcp"] = gate
            servers(("bad", _dto("http://bad/mcp")))

            with caplog.at_level("WARNING", logger="daiv.tools"):
                assert await MCPToolkit.get_tools() == []

        [record] = [r for r in caplog.records if r.name == "daiv.tools"]
        assert record.levelname == "WARNING"
        assert "SSE stream ended without a response" in record.getMessage()


@pytest.mark.django_db(transaction=True)
async def test_end_to_end_db_row_yields_prefixed_tools(gates):
    from asgiref.sync import sync_to_async
    from mcp_connectors.models import MCPServer

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
    from mcp_connectors.models import MCPServer

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
