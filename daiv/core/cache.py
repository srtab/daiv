from django.core.cache.backends.redis import RedisCache as DJRedisCache
from django.core.cache.backends.redis import RedisCacheClient as DJRedisCacheClient

from asgiref.sync import sync_to_async

Number = int | float


class RedisCacheClient(DJRedisCacheClient):
    """
    Redis cache client with lock method.
    """

    def lock(
        self,
        key,
        timeout: Number | None = None,
        sleep: Number = 0.1,
        blocking: bool = True,
        blocking_timeout: Number | None = None,
    ):
        client = self.get_client(write=True)
        return client.lock(key, timeout=timeout, sleep=sleep, blocking=blocking, blocking_timeout=blocking_timeout)


class RedisCache(DJRedisCache):
    """
    Redis cache with lock method.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._class = RedisCacheClient

    def lock(
        self,
        key,
        timeout: Number | None = None,
        sleep: Number = 0.1,
        blocking: bool = True,
        blocking_timeout: Number | None = None,
    ):
        return self._cache.lock(key, timeout=timeout, sleep=sleep, blocking=blocking, blocking_timeout=blocking_timeout)

    async def alock(
        self,
        key,
        timeout: Number | None = None,
        sleep: Number = 0.1,
        blocking: bool = True,
        blocking_timeout: Number | None = None,
    ):
        return await sync_to_async(self.lock)(
            key, timeout=timeout, sleep=sleep, blocking=blocking, blocking_timeout=blocking_timeout
        )
