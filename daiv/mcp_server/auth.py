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


async def _user_from_oauth_token(token: str) -> User | None:
    """Resolve the active user for an OAuth access token, or None if it can't be resolved."""
    token_checksum = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        oauth_token = await OAuthAccessToken.objects.select_related("user").aget(token_checksum=token_checksum)
    except OAuthAccessToken.DoesNotExist:
        return None
    except Exception:
        logger.exception("Failed to resolve user from OAuth token")
        raise

    if not oauth_token.user.is_active:
        logger.warning("OAuth token used by an inactive user during user resolution")
        return None
    return oauth_token.user


async def _active_api_key(token: str) -> APIKey | None:
    """Resolve a usable API key whose user is active, or None. Shared by the verifier and
    execution-time resolution so the lookup + active-user gate lives in one place."""
    try:
        api_key = await APIKey.objects.get_from_key(token)
    except APIKey.DoesNotExist:
        return None
    except Exception:
        logger.exception("Failed to resolve API key")
        raise

    if not api_key.user.is_active:
        logger.warning("API key used by an inactive user")
        return None
    return api_key


async def _user_from_api_key(token: str) -> User | None:
    """Resolve the active user for an API key, or None if the key is unusable/inactive."""
    api_key = await _active_api_key(token)
    return api_key.user if api_key else None


async def get_current_user() -> User | None:
    """Get the Django user for the current MCP request from the SDK-managed access-token
    contextvar (only populated during tool/resource execution, after authentication).

    Accepts OAuth2 access tokens and ``accounts.APIKey`` keys, re-resolving the user on every
    call so a token revoked — or a user deactivated — between verification and execution is
    rejected. Dispatches on the verified token's ``client_id`` (set by the verifier) so an
    API-key request doesn't pay a guaranteed-miss OAuth lookup; OAuth client ids never contain
    ``:``, so they can't collide with the ``api-key:`` prefix.
    """
    access_token = get_access_token()
    if access_token is None:
        return None
    if access_token.client_id.startswith(API_KEY_CLIENT_ID_PREFIX):
        return await _user_from_api_key(access_token.token)
    return await _user_from_oauth_token(access_token.token)


class DjangoTokenVerifier:
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
        api_key = await _active_api_key(token)
        if api_key is None:
            return None
        return MCPAccessToken(
            token=token,
            client_id=f"{API_KEY_CLIENT_ID_PREFIX}{api_key.prefix}",
            scopes=["mcp"],
            expires_at=int(api_key.expires_at.timestamp()) if api_key.expires_at else None,
        )
