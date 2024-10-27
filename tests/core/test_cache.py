"""Unit tests for cache functionality."""
import pytest
from unittest.mock import MagicMock, patch

from core.cache import RedisCacheClient, RedisCache


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
            "test_key",
            timeout=None,
            sleep=0.1,
            blocking=True,
            blocking_timeout=None
        )
        assert lock == mock_redis_client.lock.return_value

    def test_lock_custom_params(self, cache_client, mock_redis_client):
        """Test lock method with custom parameters."""
        lock = cache_client.lock(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
        )
        
        mock_redis_client.lock.assert_called_once_with(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
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
        with patch('core.cache.RedisCacheClient') as mock_client_class:
            mock_client_class.return_value = mock_cache_client
            cache = RedisCache(params=MagicMock())
            cache._cache = mock_cache_client
            return cache

    def test_init_sets_correct_class(self):
        """Test __init__ sets the correct cache client class."""
        cache = RedisCache(params=MagicMock())
        assert cache._class == RedisCacheClient

    def test_lock_default_params(self, redis_cache, mock_cache_client):
        """Test lock method with default parameters."""
        lock = redis_cache.lock("test_key")
        
        mock_cache_client.lock.assert_called_once_with(
            "test_key",
            timeout=None,
            sleep=0.1,
            blocking=True,
            blocking_timeout=None
        )
        assert lock == mock_cache_client.lock.return_value

    def test_lock_custom_params(self, redis_cache, mock_cache_client):
        """Test lock method with custom parameters."""
        lock = redis_cache.lock(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
        )
        
        mock_cache_client.lock.assert_called_once_with(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
        )
        assert lock == mock_cache_client.lock.return_value

    @pytest.mark.asyncio
    async def test_alock_default_params(self, redis_cache, mock_cache_client):
        """Test async lock method with default parameters."""
        lock = await redis_cache.alock("test_key")
        
        mock_cache_client.lock.assert_called_once_with(
            "test_key",
            timeout=None,
            sleep=0.1,
            blocking=True,
            blocking_timeout=None
        )
        assert lock == mock_cache_client.lock.return_value

    @pytest.mark.asyncio
    async def test_alock_custom_params(self, redis_cache, mock_cache_client):
        """Test async lock method with custom parameters."""
        lock = await redis_cache.alock(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
        )
        
        mock_cache_client.lock.assert_called_once_with(
            "test_key",
            timeout=10,
            sleep=0.5,
            blocking=False,
            blocking_timeout=5
        )
        assert lock == mock_cache_client.lock.return_value
