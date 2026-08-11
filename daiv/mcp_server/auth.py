import hashlib
import logging
from typing import TYPE_CHECKING

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from oauth2_provider.models import AccessToken as OAuthAccessToken

from accounts.models import APIKey

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.mcp_server")

# client_id prefix used for MCP access tokens minted from an API key, so the SDK's
# AuthenticatedUser carries a stable, non-OAuth identity distinguishable in logs.
API_KEY_CLIENT_ID_PREFIX = "api-key:"


async def _user_from_oauth_token(token: str) -> User | None:
    """Resolve the active user for an OAuth access token, or None if it can't be resolved."""
    token_checksum = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        oauth_token = await OAuthAccessToken.objects.select_related("user").aget(token_checksum=token_checksum)
    except OAuthAccessToken.DoesNotExist:
        return None

    if not oauth_token.user.is_active:
        logger.warning("OAuth token used by an inactive user during user resolution")
        return None
    return oauth_token.user


async def _user_from_api_key(token: str) -> User | None:
    """Resolve the active user for an API key, or None if the key is unusable/inactive."""
    try:
        api_key = await APIKey.objects.get_from_key(token)
    except APIKey.DoesNotExist:
        return None

    if not api_key.user.is_active:
        logger.warning("API key used by an inactive user during user resolution")
        return None
    return api_key.user


async def get_current_user() -> User | None:
    """Get the Django user associated with the current MCP request.

    Derives the user from the SDK-managed access token contextvar, so it is only available
    during MCP tool/resource execution after successful authentication. Handles both OAuth2
    access tokens and ``accounts.APIKey`` keys; the re-lookup (rather than trusting the
    verifier's result) re-validates revocation/expiry at execution time for both.
    """
    access_token = get_access_token()
    if access_token is None:
        return None

    user = await _user_from_oauth_token(access_token.token)
    if user is None:
        user = await _user_from_api_key(access_token.token)
    if user is None:
        logger.warning("Token not found during user resolution (may have been revoked)")
    return user


class DjangoOAuthTokenVerifier:
    """MCP TokenVerifier backed by django-oauth-toolkit's AccessToken and ``accounts.APIKey``.

    Bearer tokens are verified first as OAuth2 access tokens, then as API keys. Both grant the
    ``mcp`` scope so the SDK's ``required_scopes`` gate passes; API keys are per-user and carry
    no scopes of their own, so any usable key authenticates against MCP (same blanket access it
    already has on the REST Jobs/Chat endpoints).
    """

    async def verify_token(self, token: str) -> MCPAccessToken | None:
        return await self._verify_oauth_token(token) or await self._verify_api_key(token)

    async def _verify_oauth_token(self, token: str) -> MCPAccessToken | None:
        token_checksum = hashlib.sha256(token.encode("utf-8")).hexdigest()
        try:
            access_token = await OAuthAccessToken.objects.select_related("application", "user").aget(
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

        if access_token.user is None or not access_token.user.is_active:
            logger.warning("OAuth token used for MCP access belongs to a missing or inactive user")
            return None

        return MCPAccessToken(
            token=token,
            client_id=access_token.application.client_id,
            scopes=access_token.scope.split() if access_token.scope else [],
            expires_at=int(access_token.expires.timestamp()) if access_token.expires else None,
        )

    async def _verify_api_key(self, token: str) -> MCPAccessToken | None:
        try:
            api_key = await APIKey.objects.get_from_key(token)
        except APIKey.DoesNotExist:
            return None
        except Exception:
            logger.exception("Failed to validate API key against database")
            raise

        if not api_key.user.is_active:
            logger.warning("API key used for MCP access belongs to an inactive user")
            return None

        return MCPAccessToken(
            token=token,
            client_id=f"{API_KEY_CLIENT_ID_PREFIX}{api_key.prefix}",
            scopes=["mcp"],
            expires_at=int(api_key.expires_at.timestamp()) if api_key.expires_at else None,
        )
