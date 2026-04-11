import hashlib
import logging
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from oauth2_provider.models import AccessToken as OAuthAccessToken

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.mcp_server")


async def get_current_user() -> User | None:
    """Get the Django user associated with the current MCP request.

    Derives the user from the SDK-managed access token contextvar,
    so it is only available during MCP tool/resource execution after successful authentication.
    """
    access_token = get_access_token()
    if access_token is None:
        return None

    token_checksum = hashlib.sha256(access_token.token.encode("utf-8")).hexdigest()
    try:
        oauth_token = await OAuthAccessToken.objects.select_related("user").aget(token_checksum=token_checksum)
    except OAuthAccessToken.DoesNotExist:
        return None

    return oauth_token.user


class DjangoOAuthTokenVerifier:
    """MCP TokenVerifier backed by django-oauth-toolkit's AccessToken model."""

    async def verify_token(self, token: str) -> MCPAccessToken | None:
        token_checksum = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            access_token = await OAuthAccessToken.objects.select_related("application").aget(
                token_checksum=token_checksum
            )
        except OAuthAccessToken.DoesNotExist:
            return None
        except Exception:
            logger.exception("Failed to validate OAuth token against database")
            raise

        if access_token.is_expired():
            logger.debug("Expired OAuth2 token used for MCP access")
            return None

        if not access_token.allow_scopes(["mcp"]):
            logger.debug("Token missing 'mcp' scope for MCP access")
            return None

        if not access_token.application:
            logger.warning("OAuth token used for MCP access has no associated application")
            return None

        return MCPAccessToken(
            token=token,
            client_id=access_token.application.client_id,
            scopes=access_token.scope.split() if access_token.scope else [],
            expires_at=int(access_token.expires.timestamp()) if access_token.expires else None,
        )
