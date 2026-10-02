from django.core.cache import cache

import pytest


@pytest.fixture(autouse=True)
def _clear_jobs_throttle_cache():
    """MCP submit_job shares the JobsRateThrottle per-user cache bucket with the REST/chat
    endpoints; clear it per test so ordering can't produce spurious 429-style rejections."""
    cache.clear()
    yield
    cache.clear()
