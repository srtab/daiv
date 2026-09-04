from unittest.mock import AsyncMock, Mock, patch

import pytest

from accounts.models import User
from accounts.utils import aget_platform_identity, resolve_user
from codebase.base import GitPlatform


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


class TestProvenActingUserId:
    """``resolve_user`` matches platform username and email before social uid, so the DAIV account
    it returns may never have linked that platform identity. That is fine for choosing an MR
    assignee and not fine for anything that spends the account's own credentials — which now
    includes the personal MCP servers a run loads."""

    pytestmark = pytest.mark.django_db(transaction=True)

    async def test_an_authenticated_run_needs_no_platform_proof(self, user):
        """Chat and job runs carry no platform uid: the DAIV session already proved who they are."""
        from accounts.utils import aproven_acting_user_id

        assert await aproven_acting_user_id(user.pk, None, GitPlatform.GITLAB) == user.pk

    async def test_a_run_with_no_acting_user_stays_anonymous(self):
        from accounts.utils import aproven_acting_user_id

        assert await aproven_acting_user_id(None, "77", GitPlatform.GITLAB) is None

    async def test_a_webhook_uid_matching_a_linked_account_is_proven(self, user):
        from allauth.socialaccount.models import SocialAccount

        from accounts.utils import aproven_acting_user_id

        await SocialAccount.objects.acreate(user=user, provider="gitlab", uid="77")
        assert await aproven_acting_user_id(user.pk, "77", GitPlatform.GITLAB) == user.pk

    async def test_a_username_collision_is_not_an_identity(self, user):
        """The platform user resolved to this DAIV account by username or email alone. Nothing
        links them, so the run must not act as them."""
        from accounts.utils import aproven_acting_user_id

        assert await aproven_acting_user_id(user.pk, "77", GitPlatform.GITLAB) is None

    async def test_a_uid_linked_to_someone_else_is_not_an_identity(self, user, db):
        from allauth.socialaccount.models import SocialAccount

        from accounts.utils import aproven_acting_user_id

        other = await User.objects.acreate_user(username="mallory", email="m@test.com", password="pw")  # noqa: S106
        await SocialAccount.objects.acreate(user=other, provider="gitlab", uid="77")
        assert await aproven_acting_user_id(user.pk, "77", GitPlatform.GITLAB) is None
