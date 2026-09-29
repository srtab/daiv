"""In-process MCP servers for exercising DAIV's MCP client over ``httpx2.ASGITransport``."""

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from automation.agent.mcp.client import StatusTrackingClient, build_client
from automation.agent.mcp.errors import status_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

type Reply = tuple[int, bytes, bytes]
type Route = Callable[[dict | None], Reply]

APP_TOOLS = {"echo", "write", "plain", "boom", "capabilities"}


def http_status(code: int) -> Route:
    return lambda request: (code, b"text/plain", b"")


def jsonrpc_error(message: str) -> Route:
    """Answer HTTP 200 carrying a JSON-RPC error for the request."""
    return lambda request: (
        200,
        b"application/json",
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": message}}).encode(),
    )


def empty_event_stream() -> Route:
    """Open an SSE response and close it before any event: the stream drops mid-request."""
    return lambda request: (200, b"text/event-stream", b"")


# mcp 1.x servers answer the modern ``server/discover`` probe with a 400 and expect ``initialize``.
LEGACY_SERVER: dict[str, Route] = {
    "server/discover": lambda request: (
        400,
        b"application/json",
        b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}',
    )
}


def build_app() -> MCPServer:
    server = MCPServer(name="daiv-test")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    @server.tool(annotations=ToolAnnotations(read_only_hint=False))
    def write(text: str) -> str:
        """Pretend to mutate something."""
        return text

    @server.tool()
    def plain() -> str:
        """A tool without annotations."""
        return "plain"

    @server.tool()
    def boom() -> str:
        """Always fails."""
        raise ValueError("kaboom")

    @server.tool()
    def capabilities(ctx: Context) -> str:
        """Report the elicitation capability the client advertised."""
        params = ctx.request_context.session.client_params
        return "elicitation" if params and params.capabilities.elicitation else "none"

    return server


class Gate:
    """ASGI wrapper that forwards to the app, or answers a fixed response, or never answers.

    ``routes`` answer single requests instead: keyed by JSON-RPC method for a POST, by HTTP method otherwise.
    """

    def __init__(
        self,
        app,
        *,
        status: int | None = None,
        body: bytes = b"",
        content_type: bytes = b"text/plain",
        hang: bool = False,
        routes: dict[str, Route] | None = None,
    ):
        self.app = app
        self.status = status
        self.body = body
        self.content_type = content_type
        self.hang = hang
        self.routes = routes or {}
        self.request_headers: list[dict[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.request_headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        if self.hang:
            await asyncio.Event().wait()
        if self.status is not None:
            await self._reply(send, (self.status, self.content_type, self.body))
            return
        request, receive = await self._read_jsonrpc(scope, receive)
        route = self.routes.get(request["method"] if request and "method" in request else scope["method"])
        if route is None:
            await self.app(scope, receive, send)
            return
        await self._reply(send, route(request))

    @staticmethod
    async def _read_jsonrpc(scope, receive):
        if scope["method"] != "POST":
            return None, receive
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = json.loads(body) if body else None
        return (request if isinstance(request, dict) else None), replay

    @staticmethod
    async def _reply(send, reply: Reply) -> None:
        code, content_type, body = reply
        await send({"type": "http.response.start", "status": code, "headers": [(b"content-type", content_type)]})
        await send({"type": "http.response.body", "body": body})


@asynccontextmanager
async def serve(server: MCPServer | None = None, *, stateless: bool = True, **gate_options) -> AsyncIterator[Gate]:
    app = (server or build_app()).streamable_http_app(
        stateless_http=stateless, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    async with app.router.lifespan_context(app):
        yield Gate(app, **gate_options)


def client_for(
    app, *, transport: str = "http", url: str = "http://test/mcp", headers: dict[str, str] | None = None
) -> StatusTrackingClient:
    return build_client(transport, url, headers, http_transport=httpx2.ASGITransport(app=app))


def status_error(status: int, url: str = "http://x/mcp") -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", url)
    response = httpx2.Response(status, request=request)
    return httpx2.HTTPStatusError(status_message(response), request=request, response=response)
