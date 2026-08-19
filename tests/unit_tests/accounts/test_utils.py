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
async def test_resolve_user_social_account_takes_priority_over_username(user):
    """The verified provider+uid link must win over a webhook-claimed username.

    A platform user sharing a victim's username must not be misattributed to the
    victim: the social account for the provider+uid is the verified identity.
    """
    from allauth.socialaccount.models import SocialAccount

    other_user = await User.objects.acreate_user(
        username="other",
        email="other@test.com",
        password="testpass",  # noqa: S106
    )
    await SocialAccount.objects.acreate(user=other_user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345, username="testuser")

    assert result is not None
    assert result.pk == other_user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_social_account_takes_priority_over_email(user):
    """The verified provider+uid link must win over a webhook-claimed email."""
    from allauth.socialaccount.models import SocialAccount

    other_user = await User.objects.acreate_user(
        username="other",
        email="other@test.com",
        password="testpass",  # noqa: S106
    )
    await SocialAccount.objects.acreate(user=other_user, provider="gitlab", uid="12345")

    result = await resolve_user("gitlab", 12345, email="test@test.com")

    assert result is not None
    assert result.pk == other_user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_falls_back_to_username_when_no_social_account(user):
    """With no social account for the provider+uid, the username is the fallback."""
    result = await resolve_user("gitlab", 12345, username="testuser")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_falls_back_to_email_when_no_social_account(user):
    """With no social account for the provider+uid, the email is the fallback."""
    result = await resolve_user("gitlab", 12345, email="test@test.com")

    assert result is not None
    assert result.pk == user.pk


@pytest.mark.django_db(transaction=True)
async def test_resolve_user_username_fallback_does_not_cross_match_social_account():
    """The username fallback is scoped: a social account for a *different* uid must
    not be reached via the username fallback path."""
    from allauth.socialaccount.models import SocialAccount

    user = await User.objects.acreate_user(
        username="daivuser",
        email="daiv@test.com",
        password="testpass",  # noqa: S106
    )
    # A social account exists for uid 12345 (a different external identity).
    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")

    # Resolving a *different* uid with a username that matches no user falls back
    # to the username lookup, which finds nothing, and never returns the 12345 user.
    result = await resolve_user("gitlab", 99999, username="nonexistent")

    assert result is None


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
