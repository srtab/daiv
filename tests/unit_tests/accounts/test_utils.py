from unittest.mock import AsyncMock, patch

import pytest

from accounts.models import User
from accounts.utils import resolve_user_from_social


@pytest.fixture
def user(db):
    return User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_from_social_returns_user(user):
    from allauth.socialaccount.models import SocialAccount

    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")

    result = await resolve_user_from_social("gitlab", 12345)

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_from_social_returns_none_when_not_found():
    result = await resolve_user_from_social("gitlab", 99999)

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_from_social_returns_none_on_db_error():
    mock_qs = AsyncMock()
    mock_qs.aget.side_effect = Exception("connection refused")

    with patch("allauth.socialaccount.models.SocialAccount.objects") as mock_objects:
        mock_objects.select_related.return_value = mock_qs
        result = await resolve_user_from_social("github", 123)

    assert result is None
