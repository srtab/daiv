import threading

from django.core.cache.backends.locmem import LocMemCache as DJLocMemCache
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


class LocMemCache(DJLocMemCache):
    """
    LocMem cache with lock method.
    """

    _app_locks: dict[str, threading.RLock] = {}
    _app_locks_guard = threading.Lock()

    def lock(
        self,
        key,
        timeout: Number | None = None,
        sleep: Number = 0.1,
        blocking: bool = True,
        blocking_timeout: Number | None = None,
    ):
        # A per-key application lock kept separate from the cache's own internal mutex, mirroring the
        # Redis backend where ``lock()`` is independent of ``get()``/``set()``. Returning ``self._lock``
        # (the non-reentrant mutex every get/set takes) self-deadlocks any locked task whose body reads
        # the cache while holding it. Reentrant so same-thread re-entry matches Redis lock ownership.
        with self._app_locks_guard:
            return self._app_locks.setdefault(key, threading.RLock())
