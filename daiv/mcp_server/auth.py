import logging
from typing import TYPE_CHECKING

from django.contrib.auth.models import AnonymousUser

from oauth2_provider.models import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("daiv.mcp_server")


class OAuthTokenAuthMiddleware:
    """
    ASGI middleware that validates OAuth2 Bearer tokens for the MCP server.

    Extracts the token from the Authorization header, validates it against
    django-oauth-toolkit's AccessToken model, and attaches the authenticated
    user to the ASGI scope.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        auth_header = request.headers.get("authorization", "")

        if not auth_header.startswith("Bearer "):
            response = JSONResponse(
                {"error": "unauthorized", "error_description": "Missing or invalid Authorization header."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
            )
            await response(scope, receive, send)
            return

        token_str = auth_header[7:]  # Strip "Bearer "
        user = await _get_user_from_token(token_str)

        if user is None or isinstance(user, AnonymousUser):
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "The access token is invalid or expired."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mcp", error="invalid_token"'},
            )
            await response(scope, receive, send)
            return

        scope["user"] = user
        await self.app(scope, receive, send)


async def _get_user_from_token(token_str: str) -> object | None:
    """Validate an OAuth2 access token and return the associated user."""
    try:
        access_token = await AccessToken.objects.select_related("user").aget(token=token_str)
    except AccessToken.DoesNotExist:
        return None

    if access_token.is_expired():
        logger.debug("Expired OAuth2 token used for MCP access")
        return None

    return access_token.user
