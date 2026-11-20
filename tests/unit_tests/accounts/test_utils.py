from unittest.mock import AsyncMock, Mock, patch

import pytest

from accounts.models import User
from accounts.utils import aget_platform_identity, resolve_linked_user
from codebase.base import GitPlatform


@pytest.fixture
def user(db):
    return User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106


@pytest.mark.django_db(transaction=True)
async def test_resolve_linked_user_ignores_a_username_or_email_match(user):
    from allauth.socialaccount.models import SocialAccount

    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")

    assert (await resolve_linked_user("gitlab", 12345)).pk == user.pk
    assert await resolve_linked_user("gitlab", 99999) is None
    assert await resolve_linked_user("github", 12345) is None


@pytest.mark.django_db(transaction=True)
async def test_resolve_linked_user_ignores_a_deactivated_user(user):
    from allauth.socialaccount.models import SocialAccount

    await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="12345")
    await User.objects.filter(pk=user.pk).aupdate(is_active=False)

    assert await resolve_linked_user("gitlab", 12345) is None


async def test_resolve_linked_user_raises_on_db_error_rather_than_answering_unlinked():
    with patch("allauth.socialaccount.models.SocialAccount.objects") as mock_objects:
        mock_objects.select_related.side_effect = RuntimeError("connection refused")

        with pytest.raises(RuntimeError, match="connection refused"):
            await resolve_linked_user("github", 123)


# Each case uses a distinct uid: allauth is unique on (provider, uid) and these async tests share
# one user, so a row surviving teardown would collide or win the oldest-link read.
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("provider", "uid", "extra_data", "expected"),
    [
        (GitPlatform.GITLAB, "101", {"id": 101, "username": "dev"}, (101, "dev")),
        # GitPlatform.GITHUB.value must equal allauth's provider id, or the lookup silently misses.
        (GitPlatform.GITHUB, "102", {"login": "octocat"}, (102, "octocat")),
        # A uid alone is still enough to assign on GitLab.
        (GitPlatform.GITLAB, "103", {}, (103, None)),
        # A legacy row holding a non-dict would otherwise raise AttributeError past the try block.
        (GitPlatform.GITLAB, "104", "", (104, None)),
        # `str()` here would ship "{'a': 1}" to the platform as an assignee.
        (GitPlatform.GITHUB, "105", {"login": {"a": 1}}, (105, None)),
        # A non-numeric uid is no GitLab user id; `int()` would accept " +7 ", isdecimal does not.
        (GitPlatform.GITLAB, " +7 ", {"username": "dev"}, (None, "dev")),
    ],
)
async def test_get_platform_identity_projects_the_stored_payload(user, provider, uid, extra_data, expected):
    from allauth.socialaccount.models import SocialAccount

    await SocialAccount.objects.acreate(user=user, provider=provider, uid=uid, extra_data=extra_data)

    identity = await aget_platform_identity(user_id=user.pk, provider=provider)

    # Field-wise, not by equality: a NamedTuple compares equal to a plain tuple, so an
    # `== PlatformIdentity(...)` assertion would still pass on an untyped tuple.
    assert (identity.uid, identity.username) == expected


@pytest.mark.django_db(transaction=True)
async def test_get_platform_identity_without_a_link(user):
    assert await aget_platform_identity(user_id=user.pk, provider=GitPlatform.GITLAB) is None


@pytest.mark.django_db(transaction=True)
async def test_get_platform_identity_returns_none_on_db_error(user):
    """Raising would abort a publish that has already pushed the branch."""
    from allauth.socialaccount.models import SocialAccount

    with patch.object(SocialAccount, "objects") as mock_objects:
        mock_objects.filter.side_effect = Exception("connection refused")

        assert await aget_platform_identity(user_id=user.pk, provider=GitPlatform.GITLAB) is None


async def test_get_platform_identity_orders_the_link_lookup():
    """allauth is unique on (provider, uid), not (user, provider), so a re-link leaves two rows and
    the oldest must win. Asserted on the query DAIV builds: SQLite answers an unordered scan in
    rowid order anyway, so no test DB can observe the difference."""
    from allauth.socialaccount.models import SocialAccount

    queryset = Mock()
    queryset.only.return_value = queryset
    queryset.order_by.return_value = queryset
    queryset.afirst = AsyncMock(return_value=None)

    with patch.object(SocialAccount, "objects") as objects:
        objects.filter.return_value = queryset
        await aget_platform_identity(user_id=1, provider=GitPlatform.GITLAB)

    queryset.order_by.assert_called_once_with("pk")
