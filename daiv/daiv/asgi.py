"""
ASGI config for daiv project.

Combines the Django ASGI application with the MCP server under /mcp.
The MCP streamable HTTP app is wrapped with OAuth2 token validation middleware.

For more information on this file, see
https://docs.djangoproject.com/en/stable/howto/deployment/asgi/
"""

import os
from typing import TYPE_CHECKING

from django.core.asgi import get_asgi_application

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "daiv.settings.production")

# Django ASGI application must be initialized before importing MCP server
# to ensure Django apps are loaded.
django_application = get_asgi_application()

_mcp_application: ASGIApp | None = None


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
            mcp_app = _get_mcp_application()
            await mcp_app(scope, receive, send)
            return

        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/mcp"):
            mcp_app = _get_mcp_application()
            await mcp_app(scope, receive, send)
        else:
            await django_application(scope, receive, send)


application = Application()
