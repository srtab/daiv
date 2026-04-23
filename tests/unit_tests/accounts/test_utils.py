from unittest.mock import patch

import pytest

from accounts.models import User
from accounts.utils import resolve_user


@pytest.fixture
def user(db):
    return User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_by_username(user):
    result = await resolve_user("gitlab", 99999, username="testuser")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_by_email(user):
    result = await resolve_user("gitlab", 99999, email="test@test.com")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_by_social_account(user):
    from allauth.socialaccount.models import SocialAccount

    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345)

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_username_takes_priority_over_social(user):
    """Username match should resolve before falling through to social account."""
    from allauth.socialaccount.models import SocialAccount

    other_user = await User.objects.acreate_user(
        username="other",
        email="other@test.com",
        password="testpass",  # noqa: S106
    )
    await SocialAccount.objects.acreate(user=other_user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345, username="testuser")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_email_takes_priority_over_social(user):
    """Email match should resolve before falling through to social account."""
    from allauth.socialaccount.models import SocialAccount

    other_user = await User.objects.acreate_user(
        username="other",
        email="other@test.com",
        password="testpass",  # noqa: S106
    )
    await SocialAccount.objects.acreate(user=other_user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345, email="test@test.com")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_falls_through_to_social_when_no_username_or_email_match():
    from allauth.socialaccount.models import SocialAccount

    user = await User.objects.acreate_user(
        username="daivuser",
        email="daiv@test.com",
        password="testpass",  # noqa: S106
    )
    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345, username="nonexistent", email="nobody@test.com")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_returns_none_when_not_found():
    result = await resolve_user("gitlab", 99999)

    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_returns_none_on_db_error():
    with patch("accounts.models.User.objects") as mock_objects:
        mock_objects.aget.side_effect = Exception("connection refused")
        result = await resolve_user("github", 123, username="someone")

    assert result is None
