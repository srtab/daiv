"""
ASGI config for daiv project.

Combines the Django ASGI application with the MCP server under /mcp.
The MCP streamable HTTP app is wrapped with OAuth2 token validation middleware.

For more information on this file, see
https://docs.djangoproject.com/en/stable/howto/deployment/asgi/
"""

import logging
import os
import threading
from typing import TYPE_CHECKING

from django.core.asgi import get_asgi_application
from django.db import close_old_connections

from asgiref.sync import sync_to_async
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "daiv.settings.production")

logger = logging.getLogger("daiv.asgi")

# Django ASGI application must be initialized before importing MCP server
# to ensure Django apps are loaded.
django_application = get_asgi_application()

_mcp_application: ASGIApp | None = None
_mcp_lock = threading.Lock()


def _get_mcp_application() -> ASGIApp:
    """
    Lazily build the MCP ASGI application with OAuth auth middleware.

    The underlying Starlette app includes a built-in lifespan that manages
    the MCP session manager. Lifespan events are forwarded through the auth
    middleware to reach it for proper startup and shutdown.
    """
    global _mcp_application
    if _mcp_application is not None:
        return _mcp_application

    with _mcp_lock:
        if _mcp_application is not None:
            return _mcp_application

        from mcp_server.auth import OAuthTokenAuthMiddleware
        from mcp_server.server import mcp

        starlette_app = mcp.streamable_http_app()
        _mcp_application = OAuthTokenAuthMiddleware(starlette_app)
        return _mcp_application


class Application:
    """Combined ASGI application that dispatches /mcp to MCP server, everything else to Django."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # Forward lifespan events to the MCP Starlette app, which manages
            # the session manager startup/shutdown via its built-in lifespan.
            try:
                mcp_app = _get_mcp_application()
            except Exception:
                logger.exception("Failed to initialize MCP application during lifespan")
                # Complete the lifespan so the server doesn't hang — MCP will be
                # unavailable but Django keeps working.
                await receive()  # lifespan.startup
                await send({"type": "lifespan.startup.complete"})
                await receive()  # lifespan.shutdown
                await send({"type": "lifespan.shutdown.complete"})
                return
            await mcp_app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (path == "/mcp" or path.startswith("/mcp/")):
            try:
                mcp_app = _get_mcp_application()
            except Exception:
                logger.exception("MCP application unavailable")
                if scope["type"] == "http":
                    response = JSONResponse({"error": "MCP endpoint is temporarily unavailable."}, status_code=503)
                    await response(scope, receive, send)
                return
            await _dispatch_with_connection_management(mcp_app, scope, receive, send)
        else:
            await django_application(scope, receive, send)


async def _dispatch_with_connection_management(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """
    Dispatch to a non-Django ASGI app with Django database connection lifecycle management.

    Django's request_started/request_finished signals call close_old_connections() to reset
    health-check state and return stale connections to the pool. Non-Django ASGI sub-apps
    bypass these signals, so database connections cached in sync_to_async worker threads
    never get their health_check_done flag reset — causing OperationalError on stale connections.

    This mirrors Django's own request lifecycle by calling close_old_connections() before and
    after dispatching, in the same thread used by sync_to_async (thread_sensitive=True by default).
    """
    await sync_to_async(close_old_connections)()
    try:
        await app(scope, receive, send)
    finally:
        try:
            await sync_to_async(close_old_connections)()
        except Exception:
            logger.warning("Failed to close old database connections after MCP dispatch", exc_info=True)


application = Application()
