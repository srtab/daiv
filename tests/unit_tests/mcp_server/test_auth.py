from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from mcp.server.auth.provider import AccessToken as MCPAccessToken
from mcp_server.auth import DjangoOAuthTokenVerifier, get_current_user

from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106


@pytest.fixture
def oauth_app(db):
    from oauth2_provider.models import Application

    return Application.objects.create(
        name="test-app",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="http://localhost/callback",
    )


@pytest.fixture
def access_token(user, oauth_app):
    from oauth2_provider.models import AccessToken

    return AccessToken.objects.create(
        user=user,
        token="test-valid-token",  # noqa: S106
        application=oauth_app,
        expires=timezone.now() + timedelta(hours=1),
        scope="mcp",
    )


@pytest.fixture
def expired_token(user, oauth_app):
    from oauth2_provider.models import AccessToken

    return AccessToken.objects.create(
        user=user,
        token="test-expired-token",  # noqa: S106
        application=oauth_app,
        expires=timezone.now() - timedelta(hours=1),
        scope="mcp",
    )


@pytest.fixture
def wrong_scope_token(user, oauth_app):
    from oauth2_provider.models import AccessToken

    return AccessToken.objects.create(
        user=user,
        token="test-wrong-scope-token",  # noqa: S106
        application=oauth_app,
        expires=timezone.now() + timedelta(hours=1),
        scope="read",
    )


@pytest.fixture
def no_app_token(user):
    from oauth2_provider.models import AccessToken

    return AccessToken.objects.create(
        user=user,
        token="test-no-app-token",  # noqa: S106
        application=None,
        expires=timezone.now() + timedelta(hours=1),
        scope="mcp",
    )


@pytest.fixture
def verifier():
    return DjangoOAuthTokenVerifier()


@pytest.mark.django_db(transaction=True)
async def test_valid_token_returns_access_token(verifier, access_token, oauth_app):
    result = await verifier.verify_token("test-valid-token")

    assert result is not None
    assert result.token == "test-valid-token"  # noqa: S105
    assert result.client_id == oauth_app.client_id
    assert result.scopes == ["mcp"]
    assert result.expires_at is not None


@pytest.mark.django_db(transaction=True)
async def test_nonexistent_token_returns_none(verifier):
    result = await verifier.verify_token("nonexistent-token")

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_expired_token_returns_none(verifier, expired_token):
    result = await verifier.verify_token("test-expired-token")

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_wrong_scope_token_returns_none(verifier, wrong_scope_token):
    result = await verifier.verify_token("test-wrong-scope-token")

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_token_without_application_returns_none(verifier, no_app_token):
    result = await verifier.verify_token("test-no-app-token")

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_database_error_propagates(verifier):
    from django.db import OperationalError

    mock_qs = AsyncMock()
    mock_qs.aget.side_effect = OperationalError("connection refused")

    with patch("mcp_server.auth.OAuthAccessToken.objects") as mock_objects, pytest.raises(OperationalError):
        mock_objects.select_related.return_value = mock_qs
        await verifier.verify_token("any-token")


@pytest.mark.django_db(transaction=True)
async def test_get_current_user_returns_user(access_token, user):
    mcp_token = MCPAccessToken(token="test-valid-token", client_id="test", scopes=["mcp"])  # noqa: S106

    with patch("mcp_server.auth.get_access_token", return_value=mcp_token):
        result = await get_current_user()

    assert result is not None
    assert result.pk == user.pk


async def test_get_current_user_returns_none_without_token():
    with patch("mcp_server.auth.get_access_token", return_value=None):
        result = await get_current_user()

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_get_current_user_returns_none_when_token_deleted(access_token):
    """Token was valid at auth time but deleted before get_current_user runs (TOCTOU)."""
    mcp_token = MCPAccessToken(token="test-valid-token", client_id="test", scopes=["mcp"])  # noqa: S106

    # Delete the token to simulate revocation between verify_token and get_current_user
    await access_token.adelete()

    with patch("mcp_server.auth.get_access_token", return_value=mcp_token):
        result = await get_current_user()

    assert result is None
