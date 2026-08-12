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

API_KEY_CLIENT_ID_PREFIX = "api-key:"


class APIKeyAccessToken(MCPAccessToken):
    """Marker subclass identifying tokens verified as ``accounts.APIKey`` keys, so
    execution-time resolution can dispatch on type instead of overloading ``client_id``
    (which is a free-form field an OAuth application could legitimately collide with)."""


async def _oauth_token_by_checksum(token: str, *select_related: str) -> OAuthAccessToken | None:
    token_checksum = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        return await OAuthAccessToken.objects.select_related(*select_related).aget(token_checksum=token_checksum)
    except OAuthAccessToken.DoesNotExist:
        return None


async def _user_from_oauth_token(token: str) -> User | None:
    """Resolve the active user for an OAuth access token, or None if it can't be resolved."""
    oauth_token = await _oauth_token_by_checksum(token, "user")
    if oauth_token is None:
        logger.warning("OAuth token not found during user resolution (token may have been revoked)")
        return None
    if not oauth_token.user.is_active:
        logger.warning("OAuth token used by an inactive user during user resolution")
        return None
    return oauth_token.user


async def _user_from_api_key(token: str) -> User | None:
    """Resolve the active user for an API key, or None if the key is unusable/inactive."""
    api_key = await APIKey.objects.get_active_key(token)
    return api_key.user if api_key else None


async def get_current_user() -> User | None:
    """Get the Django user for the current MCP request from the SDK-managed access-token
    contextvar (only populated during tool/resource execution, after authentication).

    Accepts OAuth2 access tokens and ``accounts.APIKey`` keys, re-resolving the user on every
    call so a token revoked — or a user deactivated — between verification and execution is
    rejected. Dispatches on the verified token's type (``APIKeyAccessToken`` marker set by the
    verifier) so an API-key request doesn't pay a guaranteed-miss OAuth lookup.

    Unexpected resolution errors (e.g. a DB outage) propagate — callers log and translate
    them into their tool's error contract.
    """
    access_token = get_access_token()
    if access_token is None:
        return None
    if isinstance(access_token, APIKeyAccessToken):
        return await _user_from_api_key(access_token.token)
    return await _user_from_oauth_token(access_token.token)


class DjangoTokenVerifier:
    """MCP TokenVerifier backed by django-oauth-toolkit's AccessToken and ``accounts.APIKey``.

    Bearer tokens route by shape: API keys always contain ``.`` (``prefix.secret``) while
    django-oauth-toolkit's default generator never emits one, so each request pays exactly one
    lookup. A custom ``ACCESS_TOKEN_GENERATOR`` emitting dots would break this routing.

    Both token kinds grant the ``mcp`` scope so the SDK's ``required_scopes`` gate passes; API
    keys are per-user and carry no scopes of their own, so any usable key authenticates against
    MCP (same blanket access it already has on the REST Jobs/Chat endpoints).
    """

    async def verify_token(self, token: str) -> MCPAccessToken | None:
        try:
            if "." in token:
                return await self._verify_api_key(token)
            return await self._verify_oauth_token(token)
        except Exception:
            logger.exception("Failed to verify MCP bearer token")
            raise

    async def _verify_oauth_token(self, token: str) -> MCPAccessToken | None:
        access_token = await _oauth_token_by_checksum(token, "application", "user")
        if access_token is None:
            return None

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

    async def _verify_api_key(self, token: str) -> APIKeyAccessToken | None:
        api_key = await APIKey.objects.get_active_key(token)
        if api_key is None:
            return None
        return APIKeyAccessToken(
            token=token,
            client_id=f"{API_KEY_CLIENT_ID_PREFIX}{api_key.prefix}",
            scopes=["mcp"],
            expires_at=int(api_key.expires_at.timestamp()) if api_key.expires_at else None,
        )
