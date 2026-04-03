import logging
from typing import TYPE_CHECKING

from oauth2_provider.models import AccessToken
from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from accounts.models import User

logger = logging.getLogger("daiv.mcp_server")


class OAuthTokenAuthMiddleware:
    """
    ASGI middleware that validates OAuth2 Bearer tokens for the MCP server.

    Extracts the token from the Authorization header, validates it against
    django-oauth-toolkit's AccessToken model, and attaches the authenticated
    user to the ASGI scope. Non-HTTP scope types (e.g., lifespan) are passed
    through without authentication.
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

        token_str = auth_header[7:]
        try:
            user = await _get_user_from_token(token_str)
        except Exception:
            logger.exception("Failed to validate OAuth token")
            response = JSONResponse(
                {"error": "server_error", "error_description": "Authentication service unavailable."}, status_code=503
            )
            await response(scope, receive, send)
            return

        if user is None:
            response = JSONResponse(
                {"error": "invalid_token", "error_description": "The access token is invalid or expired."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="mcp", error="invalid_token"'},
            )
            await response(scope, receive, send)
            return

        scope["user"] = user
        await self.app(scope, receive, send)


async def _get_user_from_token(token_str: str) -> User | None:
    """Validate an OAuth2 access token and return the associated user."""
    try:
        access_token = await AccessToken.objects.select_related("user").aget(token=token_str)
    except AccessToken.DoesNotExist:
        return None

    if access_token.is_expired():
        logger.debug("Expired OAuth2 token used for MCP access")
        return None

    if not access_token.allow_scopes(["mcp"]):
        logger.debug("Token missing 'mcp' scope for MCP access")
        return None

    return access_token.user
