from django.core.cache import cache

import pytest


@pytest.fixture(autouse=True)
def _clear_jobs_throttle_cache():
    """``JobsRateThrottle`` keeps per-user buckets in Django's cache; without clearing,
    later tests in the same file can hit 429 because earlier tests already exhausted
    the same user's budget. Clearing makes ordering irrelevant."""
    cache.clear()
    yield
    cache.clear()
