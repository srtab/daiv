"""Unit tests for cache functionality."""

import threading
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import caches

import pytest

from core.cache import LocMemCache, RedisCache, RedisCacheClient


class RedisCacheClientTest:
    """Tests for RedisCacheClient class."""

    @pytest.fixture
    def mock_redis_client(self):
        """Fixture for mocked Redis client."""
        client = MagicMock()
        client.lock.return_value = MagicMock()
        return client

    @pytest.fixture
    def cache_client(self, mock_redis_client):
        """Fixture for RedisCacheClient instance."""
        client = RedisCacheClient(MagicMock())
        client.get_client = MagicMock(return_value=mock_redis_client)
        return client

    def test_lock_default_params(self, cache_client, mock_redis_client):
        """Test lock method with default parameters."""
        lock = cache_client.lock("test_key")

        mock_redis_client.lock.assert_called_once_with(
            "test_key", timeout=None, sleep=0.1, blocking=True, blocking_timeout=None
        )
        assert lock == mock_redis_client.lock.return_value

    def test_lock_custom_params(self, cache_client, mock_redis_client):
        """Test lock method with custom parameters."""
        lock = cache_client.lock("test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5)

        mock_redis_client.lock.assert_called_once_with(
            "test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5
        )
        assert lock == mock_redis_client.lock.return_value


class RedisCacheTest:
    """Tests for RedisCache class."""

    @pytest.fixture
    def mock_cache_client(self):
        """Fixture for mocked cache client."""
        client = MagicMock()
        client.lock.return_value = MagicMock()
        return client

    @pytest.fixture
    def redis_cache(self, mock_cache_client):
        """Fixture for RedisCache instance."""
        with patch("core.cache.RedisCacheClient") as mock_client_class:
            mock_client_class.return_value = mock_cache_client
            cache = RedisCache(MagicMock(), MagicMock())
            cache._cache = mock_cache_client
            return cache

    def test_init_sets_correct_class(self):
        """Test __init__ sets the correct cache client class."""
        cache = RedisCache(MagicMock(), MagicMock())
        assert cache._class == RedisCacheClient

    def test_lock_default_params(self, redis_cache, mock_cache_client):
        """Test lock method with default parameters."""
        lock = redis_cache.lock("test_key")

        mock_cache_client.lock.assert_called_once_with(
            "test_key", timeout=None, sleep=0.1, blocking=True, blocking_timeout=None
        )
        assert lock == mock_cache_client.lock.return_value

    def test_lock_custom_params(self, redis_cache, mock_cache_client):
        """Test lock method with custom parameters."""
        lock = redis_cache.lock("test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5)

        mock_cache_client.lock.assert_called_once_with(
            "test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5
        )
        assert lock == mock_cache_client.lock.return_value

    async def test_alock_default_params(self, redis_cache, mock_cache_client):
        """Test async lock method with default parameters."""
        lock = await redis_cache.alock("test_key")

        mock_cache_client.lock.assert_called_once_with(
            "test_key", timeout=None, sleep=0.1, blocking=True, blocking_timeout=None
        )
        assert lock == mock_cache_client.lock.return_value

    async def test_alock_custom_params(self, redis_cache, mock_cache_client):
        """Test async lock method with custom parameters."""
        lock = await redis_cache.alock("test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5)

        mock_cache_client.lock.assert_called_once_with(
            "test_key", timeout=10, sleep=0.5, blocking=False, blocking_timeout=5
        )
        assert lock == mock_cache_client.lock.return_value


# Deterministic wait: never a source of correctness, only a deadlock ceiling.
_JOIN_TIMEOUT = 5.0


class TestLocMemCacheLock:
    """Pin the ``LocMemCache.lock()`` shim used by ``locked_task`` under the test cache backend.

    The shim must return a per-key application lock decoupled from the cache's own internal mutex
    (which every ``get``/``set`` takes). Returning that internal mutex — the pre-fix behaviour —
    self-deadlocked any locked task whose body reads the cache while holding the lock, while still
    having to preserve same-key cross-thread mutual exclusion (what ``locked_task`` relies on).
    """

    @pytest.fixture
    def cache(self):
        c = caches["default"]
        assert isinstance(c, LocMemCache)
        return c

    @pytest.fixture
    def key(self):
        return f"test-lock-{uuid.uuid4()}"

    def _run(self, target):
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(_JOIN_TIMEOUT)
        return t

    def test_cache_access_under_lock_does_not_self_deadlock(self, cache, key):
        done = threading.Event()

        def worker():
            with cache.lock(key):
                cache.set(f"{key}:v", 1)
                assert cache.get(f"{key}:v") == 1
            done.set()

        self._run(worker)
        assert done.is_set(), "holding cache.lock() must not block cache get/set on the same thread"

    def test_same_thread_reentrant_acquire(self, cache, key):
        done = threading.Event()

        def worker():
            with cache.lock(key), cache.lock(key):
                pass
            done.set()

        self._run(worker)
        assert done.is_set(), "same thread must be able to re-acquire the same lock key (RLock)"

    def test_same_key_is_mutually_exclusive_across_threads(self, cache, key):
        a_holds = threading.Event()
        release_a = threading.Event()
        b_acquired = threading.Event()

        def holder_a():
            with cache.lock(key):
                a_holds.set()
                release_a.wait(_JOIN_TIMEOUT)

        def contender_b():
            a_holds.wait(_JOIN_TIMEOUT)
            with cache.lock(key):
                b_acquired.set()

        ta = threading.Thread(target=holder_a, daemon=True)
        tb = threading.Thread(target=contender_b, daemon=True)
        ta.start()
        assert a_holds.wait(_JOIN_TIMEOUT), "holder failed to acquire the lock"
        tb.start()

        assert not b_acquired.wait(0.2), "second thread acquired the same key while it was held"
        release_a.set()
        assert b_acquired.wait(_JOIN_TIMEOUT), "second thread never acquired after release"
        ta.join(_JOIN_TIMEOUT)
        tb.join(_JOIN_TIMEOUT)

    def test_distinct_keys_do_not_block_each_other(self, cache, key):
        key_a, key_b = f"{key}:a", f"{key}:b"
        a_holds = threading.Event()
        release_a = threading.Event()
        b_acquired = threading.Event()

        def holder_a():
            with cache.lock(key_a):
                a_holds.set()
                release_a.wait(_JOIN_TIMEOUT)

        def contender_b():
            a_holds.wait(_JOIN_TIMEOUT)
            with cache.lock(key_b):
                b_acquired.set()

        ta = threading.Thread(target=holder_a, daemon=True)
        tb = threading.Thread(target=contender_b, daemon=True)
        ta.start()
        assert a_holds.wait(_JOIN_TIMEOUT), "holder failed to acquire lock key_a"
        tb.start()

        assert b_acquired.wait(_JOIN_TIMEOUT), "a distinct key must not block on another key"
        release_a.set()
        ta.join(_JOIN_TIMEOUT)
        tb.join(_JOIN_TIMEOUT)
