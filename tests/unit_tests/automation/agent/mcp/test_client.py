import asyncio
import logging

import httpx2
import pytest
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from langchain_core.tools import StructuredTool
from langgraph.errors import GraphInterrupt
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import Icon

from automation.agent.mcp.client import StatusRecorder, build_client, list_tools, list_tools_and_icon, pick_icon_src
from automation.agent.mcp.errors import classify
from tests.unit_tests.automation.agent.mcp.helpers import (
    APP_TOOLS,
    LEGACY_SERVER,
    client_for,
    http_status,
    jsonrpc_error,
    serve,
)


class TestBuildClient:
    def test_http_uses_streamable_http_transport(self):
        client = build_client("http", "http://demo.test/mcp", {"X-Key": "v"})

        assert isinstance(client.transport, StreamableHttpTransport)
        assert client.transport.url == "http://demo.test/mcp"
        assert client.transport.headers == {"X-Key": "v"}

    def test_sse_uses_sse_transport(self):
        client = build_client("sse", "http://demo.test/sse", None)

        assert isinstance(client.transport, SSETransport)
        assert client.transport.url == "http://demo.test/sse"

    def test_unsupported_transport_raises(self):
        with pytest.raises(ValueError, match="Unsupported transport: 'stdio'"):
            build_client("stdio", "http://demo.test", None)

    @pytest.mark.parametrize("transport", ["http", "sse"])
    def test_http_client_defaults_to_the_mcp_timeouts(self, transport):
        client = build_client(transport, "http://demo.test/mcp", None)

        http_client = client.transport.httpx_client_factory(headers={}, auth=None, follow_redirects=True)

        assert http_client.timeout == httpx2.Timeout(30.0, read=300.0)
        assert http_client.follow_redirects is True

    def test_http_client_keeps_the_timeout_the_transport_asks_for(self):
        client = build_client("sse", "http://demo.test/sse", None)

        http_client = client.transport.httpx_client_factory(timeout=httpx2.Timeout(5.0, read=60.0))

        assert http_client.timeout == httpx2.Timeout(5.0, read=60.0)


class TestStatusRecorder:
    @pytest.mark.parametrize("status", [405, 503])
    async def test_records_the_last_failed_post(self, status):
        recorder = StatusRecorder()
        response = httpx2.Response(status, request=httpx2.Request("POST", "http://demo.test/mcp"))

        await recorder(response)

        assert recorder.last is response

    async def test_ignores_successful_responses(self):
        recorder = StatusRecorder()

        await recorder(httpx2.Response(200, request=httpx2.Request("POST", "http://demo.test/mcp")))

        assert recorder.last is None

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    @pytest.mark.parametrize("status", [404, 405, 502])
    async def test_ignores_non_post_responses(self, method, status):
        recorder = StatusRecorder()

        await recorder(httpx2.Response(status, request=httpx2.Request(method, "http://demo.test/mcp")))

        assert recorder.last is None

    async def test_successful_post_clears_an_earlier_failure(self):
        recorder = StatusRecorder()
        request = httpx2.Request("POST", "http://demo.test/mcp")

        await recorder(httpx2.Response(400, request=request))
        await recorder(httpx2.Response(200, request=request))

        assert recorder.last is None


class TestListTools:
    async def test_lists_and_converts_tools(self):
        async with serve() as gate:
            tools = await list_tools(client_for(gate))
            echo = next(tool for tool in tools if tool.name == "echo")
            result = await echo.ainvoke({"text": "hi"})

        assert {tool.name for tool in tools} == APP_TOOLS
        assert result[0]["text"] == "hi"

    async def test_configured_headers_reach_the_server(self):
        async with serve() as gate:
            await list_tools(client_for(gate, headers={"X-Test": "1", "Authorization": "Bearer abc"}))

        assert any(h.get("x-test") == "1" and h.get("authorization") == "Bearer abc" for h in gate.request_headers)

    async def test_no_elicitation_capability_is_advertised(self):
        async with serve() as gate:
            tools = await list_tools(client_for(gate))
            capabilities = next(tool for tool in tools if tool.name == "capabilities")
            result = await capabilities.ainvoke({})

        assert result[0]["text"] == "none"

    @pytest.mark.parametrize(("status", "reason"), [(401, "Unauthorized"), (503, "Service Unavailable")])
    async def test_http_failure_raises_status_error(self, status, reason):
        async with serve(status=status) as gate:
            with pytest.raises(httpx2.HTTPStatusError) as exc_info:
                await list_tools(client_for(gate))

        error = exc_info.value
        assert (error.response.status_code, error.request.url) == (status, "http://test/mcp")
        assert str(error) == f"HTTP {status} {reason} for url 'http://test/mcp'"
        assert error.__cause__ is not None

    async def test_sse_http_failure_keeps_its_status(self):
        async with serve(status=503) as gate:
            with pytest.raises(BaseException) as exc_info:  # noqa: PT011
                await list_tools(client_for(gate, transport="sse", url="http://test/sse"))

        assert classify(exc_info.value).message == "HTTP 503 Service Unavailable for url 'http://test/sse'"

    async def test_lists_tools_from_a_legacy_server(self):
        async with serve(stateless=False, routes=LEGACY_SERVER) as gate:
            tools = await list_tools(client_for(gate))

        assert {tool.name for tool in tools} == APP_TOOLS

    async def test_legacy_server_tool_calls_skip_the_discover_probe(self):
        discovers = []

        def discover(request):
            discovers.append(request)
            return LEGACY_SERVER["server/discover"](request)

        async with serve(stateless=False, routes={"server/discover": discover}) as gate:
            client = client_for(gate)
            tools = await list_tools(client)
            await next(tool for tool in tools if tool.name == "echo").ainvoke({"text": "hi"})

        assert client.mode == "legacy"
        assert len(discovers) == 1

    async def test_modern_server_keeps_auto_negotiation(self):
        async with serve() as gate:
            client = client_for(gate)
            await list_tools(client)

        assert client.mode == "auto"

    async def test_protocol_error_is_not_blamed_on_the_rejected_discover_probe(self):
        routes = {**LEGACY_SERVER, "tools/list": jsonrpc_error("upstream catalog unavailable")}
        async with serve(stateless=False, routes=routes) as gate:
            with pytest.raises(MCPError, match="upstream catalog unavailable"):
                await list_tools(client_for(gate))

    async def test_teardown_failure_does_not_mask_the_request_failure(self):
        routes = {**LEGACY_SERVER, "tools/list": http_status(503), "DELETE": http_status(404)}
        async with serve(stateless=False, routes=routes) as gate:
            with pytest.raises(httpx2.HTTPStatusError) as exc_info:
                await list_tools(client_for(gate))

        assert exc_info.value.response.status_code == 503

    async def test_timeout_propagates_unchanged(self):
        async with serve(hang=True) as gate:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(list_tools(client_for(gate)), timeout=0.2)


def _tool_call(name: str, **args) -> dict:
    return {"id": "call-1", "name": name, "args": args, "type": "tool_call"}


async def _call_big(size: int):
    server = MCPServer(name="bulky")

    @server.tool()
    def big() -> str:
        """Answer with a large payload."""
        return "x" * size

    # Only a legacy server answers tool calls over SSE; a modern one replies with plain JSON.
    async with serve(server, routes=LEGACY_SERVER) as gate:
        tools = await list_tools(client_for(gate))
        return await tools[0].ainvoke(_tool_call("big"))


def _fail_write_with_503_and_others_with_a_protocol_error(request):
    if request["params"]["name"] == "write":
        return http_status(503)(request)
    return jsonrpc_error("upstream exploded")(request)


class TestToolCallFailures:
    @pytest.mark.parametrize(
        ("route", "expected", "traceback"),
        [
            pytest.param(jsonrpc_error("upstream exploded"), "upstream exploded", True, id="protocol-error"),
            pytest.param(
                http_status(503), "HTTP 503 Service Unavailable for url 'http://test/mcp'", False, id="http-error"
            ),
        ],
    )
    async def test_failure_becomes_an_error_tool_message_logged_at_error(self, caplog, route, expected, traceback):
        async with serve(routes={"tools/call": route}) as gate:
            echo = next(tool for tool in await list_tools(client_for(gate)) if tool.name == "echo")
            with caplog.at_level("WARNING", logger="daiv.tools"):
                message = await echo.ainvoke(_tool_call("echo", text="hi"))

        assert message.status == "error"
        assert expected in message.content
        assert "may have run it" not in message.content
        records = [r for r in caplog.records if r.name == "daiv.tools"]
        assert [(r.levelname, bool(r.exc_info)) for r in records] == [("ERROR", traceback)]

    async def test_failed_write_tool_warns_that_it_may_have_run(self):
        async with serve(routes={"tools/call": jsonrpc_error("upstream exploded")}) as gate:
            write = next(tool for tool in await list_tools(client_for(gate)) if tool.name == "write")
            message = await write.ainvoke(_tool_call("write", text="hi"))

        assert message.status == "error"
        assert "may have run it anyway" in message.content

    async def test_another_calls_http_failure_is_not_blamed_on_this_call(self, monkeypatch):
        async with serve(routes={"tools/call": _fail_write_with_503_and_others_with_a_protocol_error}) as gate:
            client = client_for(gate)
            tools = {tool.name: tool for tool in await list_tools(client)}
            call_tool = client.call_tool
            write_messages = []

            async def call_tool_then_let_write_fail(name, *args, **kwargs):
                try:
                    return await call_tool(name, *args, **kwargs)
                except MCPError:
                    if name == "echo":
                        write_messages.append(await tools["write"].ainvoke(_tool_call("write", text="x")))
                    raise

            monkeypatch.setattr(client, "call_tool", call_tool_then_let_write_fail)
            message = await tools["echo"].ainvoke(_tool_call("echo", text="hi"))

        assert "upstream exploded" in message.content
        assert "HTTP 503" not in message.content
        assert "HTTP 503 Service Unavailable" in write_messages[0].content

    async def test_sse_result_over_the_size_cap_becomes_an_error_tool_message(self, monkeypatch):
        monkeypatch.setattr("automation.agent.mcp.client._MAX_STREAMABLE_HTTP_EVENT_SIZE", 64 * 1024)

        message = await _call_big(128 * 1024)

        assert message.status == "error"
        assert "exceeded the 65536 byte limit" in message.content

    async def test_cancellation_propagates(self):
        server = MCPServer(name="slow")
        started = asyncio.Event()

        @server.tool()
        async def stall() -> str:
            """Never answer."""
            started.set()
            await asyncio.Event().wait()
            return "never"

        async with serve(server) as gate:
            tools = await list_tools(client_for(gate))
            call = asyncio.create_task(tools[0].ainvoke(_tool_call("stall")))
            await started.wait()
            call.cancel()

            with pytest.raises(asyncio.CancelledError):
                await call

    async def test_graph_interrupt_propagates(self, monkeypatch):
        async with serve() as gate:
            client = client_for(gate)
            echo = next(tool for tool in await list_tools(client) if tool.name == "echo")

            async def interrupt(*args, **kwargs):
                raise GraphInterrupt(())

            monkeypatch.setattr(client, "call_tool", interrupt)
            with pytest.raises(GraphInterrupt):
                await echo.ainvoke(_tool_call("echo", text="hi"))

    async def test_unexpected_tool_shape_fails_loudly(self, monkeypatch):
        async def sync_only_tool(tool, client):
            return StructuredTool.from_function(func=lambda: "x", name=tool.name, description="sync only")

        monkeypatch.setattr("automation.agent.mcp.client.as_langchain_tool", sync_only_tool)
        async with serve() as gate:
            with pytest.raises(TypeError, match="unexpected shape"):
                await list_tools(client_for(gate))

    async def test_server_reported_error_keeps_the_server_detail(self):
        async with serve() as gate:
            boom = next(tool for tool in await list_tools(client_for(gate)) if tool.name == "boom")
            message = await boom.ainvoke(_tool_call("boom"))

        assert message.status == "error"
        assert message.content[0]["text"] == "Error executing tool boom"

    async def test_sse_result_larger_than_one_mib_arrives_whole(self):
        size = 2 * 1024 * 1024

        message = await _call_big(size)

        assert message.status == "success"
        assert len(message.content[0]["text"]) == size


class TestServerLogNotifications:
    async def test_server_sent_log_messages_are_not_emitted(self):
        server = MCPServer(name="chatty")

        @server.tool()
        async def shout(ctx: Context) -> str:
            """Send an error-level log notification, then answer."""
            await ctx.error("injected by the server")
            return "done"

        emitted: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = emitted.append
        from_server = logging.getLogger("fastmcp.client.from_server")
        from_server.addHandler(handler)
        try:
            async with serve(server) as gate:
                tools = await list_tools(client_for(gate))
                result = await next(tool for tool in tools if tool.name == "shout").ainvoke({})
        finally:
            from_server.removeHandler(handler)

        assert result[0]["text"] == "done"
        assert emitted == []


@pytest.mark.parametrize(
    ("icons", "expected"),
    [
        ([Icon(src="https://x.test/a.svg")], "https://x.test/a.svg"),
        ([Icon(src="http://x.test/a.png"), Icon(src="data:image/png;base64,AAAA")], "data:image/png;base64,AAAA"),
        ([Icon(src="javascript:alert(1)")], None),
        ([Icon(src="data:image/svg+xml;base64," + "A" * 40000)], None),
        (None, None),
    ],
)
def test_pick_icon_src(icons, expected):
    assert pick_icon_src(icons) == expected


class TestListToolsAndIcon:
    @pytest.mark.parametrize("routes", [{}, LEGACY_SERVER], ids=["modern", "legacy"])
    async def test_returns_the_icon_the_server_advertises(self, routes):
        server = MCPServer(name="iconic", icons=[Icon(src="https://x.test/i.png")])
        async with serve(server, routes=routes) as gate:
            _, icon = await list_tools_and_icon(client_for(gate))

        assert icon == "https://x.test/i.png"

    async def test_no_icon_when_the_server_advertises_none(self):
        async with serve() as gate:
            tools, icon = await list_tools_and_icon(client_for(gate))

        assert icon is None
        assert {tool.name for tool in tools} == APP_TOOLS
