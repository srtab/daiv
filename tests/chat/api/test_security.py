from datetime import timedelta

from django.utils import timezone

import pytest

from accounts.models import APIKey, User
from chat.api.security import AuthBearer


@pytest.fixture
def auth():
    return AuthBearer()


@pytest.fixture
def user():
    return User.objects.create_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",  # noqa: S106
    )


@pytest.fixture
def api_key(user) -> tuple[APIKey, str]:
    return APIKey.objects.create_key(user=user, name="Test Key")


@pytest.mark.django_db
def test_authenticate_valid_key(auth: AuthBearer, user: User, api_key: tuple[APIKey, str]):
    """Test authentication with a valid API key."""
    authenticated_user = auth.authenticate(None, api_key[1])
    assert authenticated_user == user


@pytest.mark.django_db
def test_authenticate_nonexistent_key(auth: AuthBearer):
    """Test authentication with a non-existent API key."""
    authenticated_user = auth.authenticate(None, "nonexistent_key")
    assert authenticated_user is None


@pytest.mark.django_db
def test_authenticate_revoked_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with a revoked API key."""
    api_key[0].revoked = True
    api_key[0].save()
    authenticated_user = auth.authenticate(None, api_key[1])
    assert authenticated_user is None


@pytest.mark.django_db
def test_authenticate_expired_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with an expired API key."""
    api_key[0].expires_at = timezone.now() - timedelta(days=1)
    api_key[0].save()
    authenticated_user = auth.authenticate(None, api_key[1])
    assert authenticated_user is None


@pytest.mark.django_db
def test_authenticate_future_expiry_key(auth: AuthBearer, user: User, api_key: tuple[APIKey, str]):
    """Test authentication with a key that expires in the future."""
    api_key[0].expires_at = timezone.now() + timedelta(days=1)
    api_key[0].save()
    authenticated_user = auth.authenticate(None, api_key[1])
    assert authenticated_user == user
