from types import SimpleNamespace

from django.core.cache import cache

import pytest

from accounts.models import User
from core.api.throttling import JobsRateThrottle, SpendRateThrottle


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _authed(user):
    return SimpleNamespace(auth=user, META={})


def test_users_sharing_a_display_name_get_separate_buckets():
    first = User(pk=1, username="alex1", name="Alex")
    second = User(pk=2, username="alex2", name="Alex")
    assert str(first) == str(second)

    assert JobsRateThrottle("1/hour").allow_request(_authed(first)) is True
    assert JobsRateThrottle("1/hour").allow_request(_authed(second)) is True


def test_renaming_does_not_reset_the_bucket():
    user = User(pk=1, username="alex", name="Alex")
    assert JobsRateThrottle("1/hour").allow_request(_authed(user)) is True

    user.name = "Someone Else"

    assert JobsRateThrottle("1/hour").allow_request(_authed(user)) is False


def test_anonymous_requests_are_bucketed_by_ip():
    def anonymous(ip):
        return SimpleNamespace(auth=None, META={"REMOTE_ADDR": ip})

    assert JobsRateThrottle("1/hour").allow_request(anonymous("10.0.0.1")) is True
    assert JobsRateThrottle("1/hour").allow_request(anonymous("10.0.0.1")) is False
    assert JobsRateThrottle("1/hour").allow_request(anonymous("10.0.0.2")) is True


def test_spend_reads_allow_sixty_a_minute_per_user_in_a_bucket_of_their_own():
    user, other = User(pk=1, username="a"), User(pk=2, username="b")

    assert all(SpendRateThrottle().allow_request(_authed(user)) for _ in range(60))
    assert SpendRateThrottle().allow_request(_authed(user)) is False
    assert SpendRateThrottle().allow_request(_authed(other)) is True
    assert JobsRateThrottle("1/hour").allow_request(_authed(user)) is True


def _ci_identity(actor_uid: str, jti: str):
    from datetime import UTC, datetime

    from accounts.api.ci_identity import CIIdentity
    from codebase.base import GitPlatform

    return CIIdentity(
        provider=GitPlatform.GITLAB, repo_id="group/project", repo_platform_id=1, actor_uid=actor_uid,
        actor_username="pusher", user=None, sha=None, jti=jti, expires_at=datetime.now(UTC),
    )  # fmt: skip


def test_ci_identity_tokens_are_bucketed_by_their_triggering_account():
    assert JobsRateThrottle("1/hour").allow_request(_authed(_ci_identity("9", "a"))) is True
    assert JobsRateThrottle("1/hour").allow_request(_authed(_ci_identity("9", "b"))) is False
    assert JobsRateThrottle("1/hour").allow_request(_authed(_ci_identity("10", "c"))) is True
    assert JobsRateThrottle("1/hour").allow_request(_authed(User(pk=9, username="nine"))) is True
