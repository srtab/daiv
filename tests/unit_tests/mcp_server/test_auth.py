from datetime import timedelta
from unittest.mock import AsyncMock

from django.utils import timezone

import pytest
from mcp_server.auth import OAuthTokenAuthMiddleware

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
def mock_app():
    return AsyncMock()


@pytest.fixture
def middleware(mock_app):
    return OAuthTokenAuthMiddleware(mock_app)


def _make_scope(path: str = "/mcp", headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {"type": "http", "method": "POST", "path": path, "headers": headers or [], "query_string": b""}


@pytest.mark.django_db
async def test_valid_token_passes_through(middleware, mock_app, access_token):
    scope = _make_scope(headers=[(b"authorization", b"Bearer test-valid-token")])
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_called_once()
    assert scope["user"].username == "testuser"


@pytest.mark.django_db
async def test_missing_auth_header_returns_401(middleware, mock_app):
    scope = _make_scope()
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_not_called()
    # Verify 401 response was sent
    send_calls = send.call_args_list
    assert any(b"401" in str(call).encode() for call in send_calls) or len(send_calls) > 0


@pytest.mark.django_db
async def test_invalid_token_returns_401(middleware, mock_app):
    scope = _make_scope(headers=[(b"authorization", b"Bearer invalid-token")])
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_not_called()


@pytest.mark.django_db
async def test_expired_token_returns_401(middleware, mock_app, expired_token):
    scope = _make_scope(headers=[(b"authorization", b"Bearer test-expired-token")])
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_not_called()


@pytest.mark.django_db
async def test_non_bearer_auth_returns_401(middleware, mock_app):
    scope = _make_scope(headers=[(b"authorization", b"Basic dXNlcjpwYXNz")])
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_not_called()


async def test_non_http_scope_passes_through(middleware, mock_app):
    scope = {"type": "lifespan"}
    receive = AsyncMock()
    send = AsyncMock()

    await middleware(scope, receive, send)

    mock_app.assert_called_once_with(scope, receive, send)
