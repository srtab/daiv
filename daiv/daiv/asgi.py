"""
ASGI config for daiv project.

Combines the Django ASGI application with the MCP server under /mcp/.
The MCP streamable HTTP app is wrapped with OAuth2 token validation middleware.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "daiv.settings.production")

# Django ASGI application must be initialized before importing MCP server
# to ensure Django apps are loaded.
django_application = get_asgi_application()

_mcp_application = None


def _get_mcp_application():
    """
    Lazily build the MCP ASGI application with OAuth auth middleware.

    The MCP SDK's streamable_http_app() returns a Starlette app with a route
    at /mcp (the default streamable_http_path). We wrap it with our OAuth
    token validation middleware.
    """
    global _mcp_application
    if _mcp_application is not None:
        return _mcp_application

    from mcp_server.auth import OAuthTokenAuthMiddleware
    from mcp_server.server import mcp

    starlette_app = mcp.streamable_http_app()
    _mcp_application = OAuthTokenAuthMiddleware(starlette_app)
    return _mcp_application


async def application(scope, receive, send):
    """Combined ASGI application that dispatches /mcp to MCP server, everything else to Django."""
    if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/mcp"):
        mcp_app = _get_mcp_application()
        await mcp_app(scope, receive, send)
    else:
        await django_application(scope, receive, send)
