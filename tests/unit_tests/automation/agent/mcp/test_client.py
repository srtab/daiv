import asyncio
import logging

import httpx2
import pytest
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError

from automation.agent.mcp.client import FailedResponse, MCPHTTPStatusError, StatusRecorder, build_client, list_tools
from automation.agent.mcp.errors import classify
from tests.unit_tests.automation.agent.mcp.helpers import LEGACY_SERVER, client_for, http_status, jsonrpc_error, serve

APP_TOOLS = {"echo", "write", "plain", "boom", "structured", "capabilities"}


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
    async def test_records_the_last_failed_response(self):
        recorder = StatusRecorder()

        await recorder(httpx2.Response(503, request=httpx2.Request("POST", "http://demo.test/mcp")))

        assert recorder.last == FailedResponse(503, "Service Unavailable", "http://demo.test/mcp")

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

    async def test_records_405_on_post(self):
        recorder = StatusRecorder()

        await recorder(httpx2.Response(405, request=httpx2.Request("POST", "http://demo.test/mcp")))

        assert recorder.last is not None
        assert recorder.last.status_code == 405


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
            with pytest.raises(MCPHTTPStatusError) as exc_info:
                await list_tools(client_for(gate))

        error = exc_info.value
        assert (error.status_code, error.reason, error.url) == (status, reason, "http://test/mcp")
        assert str(error) == f"HTTP {status} {reason} for url 'http://test/mcp'"
        assert error.__cause__ is not None

    async def test_sse_http_failure_keeps_its_status(self):
        async with serve(status=503) as gate:
            with pytest.raises(BaseException) as exc_info:  # noqa: PT011
                await list_tools(client_for(gate, transport="sse", url="http://test/sse"))

        assert classify(exc_info.value).message == "HTTP 503 Service Unavailable for url 'http://test/sse'"

    async def test_connect_failure_is_not_relabelled_by_a_stale_recorded_status(self):
        client = build_client("http", "http://127.0.0.1:1/mcp", None)
        client.status_recorder.last = FailedResponse(503, "Service Unavailable", "http://elsewhere/mcp")

        with pytest.raises(Exception) as exc_info:  # noqa: PT011
            await list_tools(client)

        assert not isinstance(exc_info.value, MCPHTTPStatusError)

    async def test_lists_tools_from_a_legacy_server(self):
        async with serve(stateless=False, routes=LEGACY_SERVER) as gate:
            tools = await list_tools(client_for(gate))

        assert {tool.name for tool in tools} == APP_TOOLS

    async def test_protocol_error_is_not_blamed_on_the_rejected_discover_probe(self):
        routes = {**LEGACY_SERVER, "tools/list": jsonrpc_error("upstream catalog unavailable")}
        async with serve(stateless=False, routes=routes) as gate:
            with pytest.raises(MCPError, match="upstream catalog unavailable"):
                await list_tools(client_for(gate))

    async def test_teardown_failure_does_not_mask_the_request_failure(self):
        routes = {**LEGACY_SERVER, "tools/list": http_status(503), "DELETE": http_status(404)}
        async with serve(stateless=False, routes=routes) as gate:
            with pytest.raises(MCPHTTPStatusError) as exc_info:
                await list_tools(client_for(gate))

        assert exc_info.value.status_code == 503

    async def test_timeout_propagates_unchanged(self):
        async with serve(hang=True) as gate:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(list_tools(client_for(gate)), timeout=0.2)


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
