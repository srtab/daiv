"""
ASGI config for daiv project.

Combines the Django ASGI application with the MCP server under /mcp.
The MCP streamable HTTP app is wrapped with OAuth2 token validation middleware.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import contextlib
import logging
import os
from typing import TYPE_CHECKING

from django.core.asgi import get_asgi_application

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.types import ASGIApp, Receive, Scope, Send

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "daiv.settings.production")

logger = logging.getLogger("daiv.asgi")

# Django ASGI application must be initialized before importing MCP server
# to ensure Django apps are loaded.
django_application = get_asgi_application()

_mcp_application: ASGIApp | None = None


def _get_mcp_application() -> ASGIApp:
    """
    Lazily build the MCP ASGI application with OAuth auth middleware.

    The MCP SDK's streamable_http_app() creates a Starlette app with a route
    at /mcp. This path must match the prefix used in Application.__call__
    for request dispatching.
    """
    global _mcp_application
    if _mcp_application is not None:
        return _mcp_application

    from mcp_server.auth import OAuthTokenAuthMiddleware
    from mcp_server.server import mcp

    starlette_app = mcp.streamable_http_app()
    _mcp_application = OAuthTokenAuthMiddleware(starlette_app)
    return _mcp_application


@contextlib.asynccontextmanager
async def _lifespan(_app: object) -> AsyncIterator[None]:
    """
    Start the MCP session manager via ASGI lifespan events.

    The session manager must be running before the MCP server can handle
    requests, and must be shut down cleanly when the server stops.
    """
    from mcp_server.server import mcp

    _get_mcp_application()
    async with mcp.session_manager.run():
        yield


class Application:
    """Combined ASGI application that dispatches /mcp to MCP server, everything else to Django."""

    def __init__(self) -> None:
        self._lifespan_manager: contextlib.AbstractAsyncContextManager[None] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/mcp"):
            mcp_app = _get_mcp_application()
            await mcp_app(scope, receive, send)
        else:
            await django_application(scope, receive, send)

    async def _handle_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self._lifespan_manager = _lifespan(None)
                    await self._lifespan_manager.__aenter__()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    logger.exception("MCP server lifespan startup failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif message["type"] == "lifespan.shutdown":
                if self._lifespan_manager is not None:
                    try:
                        await self._lifespan_manager.__aexit__(None, None, None)
                    except Exception:
                        logger.exception("MCP server lifespan shutdown failed")
                await send({"type": "lifespan.shutdown.complete"})
                return


application = Application()
