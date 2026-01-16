from datetime import timedelta

from django.utils import timezone

import pytest

from accounts.models import APIKey, User
from chat.api.security import AuthBearer


@pytest.fixture
def auth():
    return AuthBearer()


@pytest.fixture
async def user():
    return await User.objects.acreate_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",  # noqa: S106
    )


@pytest.fixture
async def api_key() -> tuple[APIKey, str]:
    user = await User.objects.acreate_user(
        username="testuser",
        email="test@example.com",
        password="testpass123",  # noqa: S106
    )
    return await APIKey.objects.create_key(user=user, name="Test Key")


@pytest.mark.django_db
async def test_authenticate_valid_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with a valid API key."""
    authenticated_user = await auth.authenticate(None, api_key[1])
    assert authenticated_user == api_key[0].user
    # Delete user and key to avoid conflicts with other tests. The teardown is not deleting the user and key.
    # This only happens when running the tests with asyncio.
    await api_key[0].user.adelete()


@pytest.mark.django_db
async def test_authenticate_nonexistent_key(auth: AuthBearer):
    """Test authentication with a non-existent API key."""
    authenticated_user = await auth.authenticate(None, "nonexistent_key")
    assert authenticated_user is None


@pytest.mark.django_db
async def test_authenticate_revoked_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with a revoked API key."""
    api_key[0].revoked = True
    await api_key[0].asave()
    authenticated_user = await auth.authenticate(None, api_key[1])
    assert authenticated_user is None
    # Delete user and key to avoid conflicts with other tests. The teardown is not deleting the user and key.
    # This only happens when running the tests with asyncio.
    await api_key[0].user.adelete()


@pytest.mark.django_db
async def test_authenticate_expired_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with an expired API key."""
    api_key[0].expires_at = timezone.now() - timedelta(days=1)
    await api_key[0].asave()
    authenticated_user = await auth.authenticate(None, api_key[1])
    assert authenticated_user is None
    # Delete user and key to avoid conflicts with other tests. The teardown is not deleting the user and key.
    # This only happens when running the tests with asyncio.
    await api_key[0].user.adelete()


@pytest.mark.django_db
async def test_authenticate_future_expiry_key(auth: AuthBearer, api_key: tuple[APIKey, str]):
    """Test authentication with a key that expires in the future."""
    api_key[0].expires_at = timezone.now() + timedelta(days=1)
    await api_key[0].asave()
    authenticated_user = await auth.authenticate(None, api_key[1])
    assert authenticated_user == api_key[0].user
    # Delete user and key to avoid conflicts with other tests. The teardown is not deleting the user and key.
    # This only happens when running the tests with asyncio.
    await api_key[0].user.adelete()
