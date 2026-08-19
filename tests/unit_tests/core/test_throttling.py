"""Per-user rate throttle for the chat SSE stream endpoint.

The stream holds an ASGI connection for up to the 300s duration cap and pins a pooled
Redis connection through 15s blocking ``xread``\u00a0s, so an unbounded open rate would let
one caller exhaust worker threads and the async Redis pool. ``ChatStreamRateThrottle``
bounds the *open frequency* per authenticated user; the async client's ``max_connections``
is the hard pool cap.
"""

from __future__ import annotations

from django.core.cache import cache

import pytest

from core.api.throttling import ChatStreamRateThrottle


class _FakeRequest:
    """Minimal stand-in: ``AuthRateThrottle`` keys the bucket on ``str(request.auth)``
    and never touches the rest of the request."""

    def __init__(self, auth: str) -> None:
        self.auth = auth
        self.META: dict = {}


@pytest.fixture(autouse=True)
def _isolate_cache():
    cache.clear()
    yield
    cache.clear()


class TestChatStreamRateThrottle:
    def test_default_rate_is_thirty_per_minute(self):
        throttle = ChatStreamRateThrottle()
        assert throttle.rate == "30/min"
        assert throttle.num_requests == 30
        assert throttle.duration == 60

    def test_allows_up_to_the_limit_then_denies_the_next(self):
        throttle = ChatStreamRateThrottle(rate="2/min")
        request = _FakeRequest(auth="user-a")
        assert throttle.allow_request(request) is True
        assert throttle.allow_request(request) is True
        # Third open inside the window is denied → 429 + Retry-After.
        assert throttle.allow_request(request) is False
        assert throttle.wait() is not None
        assert throttle.wait() > 0

    def test_the_budget_is_per_user_not_shared(self):
        """Each authenticated user gets its own bucket — a second user is not throttled
        because a first user exhausted theirs."""
        throttle = ChatStreamRateThrottle(rate="1/min")
        a = _FakeRequest(auth="user-a")
        b = _FakeRequest(auth="user-b")
        assert throttle.allow_request(a) is True
        assert throttle.allow_request(a) is False  # user-a exhausted
        assert throttle.allow_request(b) is True  # user-b has its own bucket
