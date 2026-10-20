from types import SimpleNamespace

from django.core.cache import cache

import pytest

from accounts.models import User
from core.api.throttling import JobsRateThrottle


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
