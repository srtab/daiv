"""In-process MCP servers for exercising DAIV's MCP client over ``httpx2.ASGITransport``."""

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from automation.agent.mcp.client import StatusTrackingClient, build_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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
    def structured(n: int) -> dict[str, int]:
        """Return structured content."""
        return {"n": n}

    @server.tool()
    def capabilities(ctx: Context) -> str:
        """Report the elicitation capability the client advertised."""
        params = ctx.request_context.session.client_params
        return "elicitation" if params and params.capabilities.elicitation else "none"

    return server


class Gate:
    """ASGI wrapper that forwards to the app, or answers a fixed response, or never answers."""

    def __init__(
        self,
        app,
        *,
        status: int | None = None,
        body: bytes = b"",
        content_type: bytes = b"text/plain",
        hang: bool = False,
    ):
        self.app = app
        self.status = status
        self.body = body
        self.content_type = content_type
        self.hang = hang
        self.request_headers: list[dict[str, str]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.request_headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        if self.hang:
            await asyncio.Event().wait()
        if self.status is None:
            await self.app(scope, receive, send)
            return
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": [(b"content-type", self.content_type)],
        })
        await send({"type": "http.response.body", "body": self.body})


@asynccontextmanager
async def serve(server: MCPServer | None = None, **gate_options) -> AsyncIterator[Gate]:
    app = (server or build_app()).streamable_http_app(
        stateless_http=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    async with app.router.lifespan_context(app):
        yield Gate(app, **gate_options)


def client_for(
    gate: Gate, *, transport: str = "http", url: str = "http://test/mcp", headers: dict[str, str] | None = None
) -> StatusTrackingClient:
    return build_client(transport, url, headers, http_transport=httpx2.ASGITransport(app=gate))
