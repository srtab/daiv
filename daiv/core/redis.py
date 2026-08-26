"""Lazy, process-wide Redis clients.

Every Redis feature that isn't the cache — the chat event relay (``chat.api.relay``)
and the UI event bus (``core.ui_events``) — resolves its client here, so "is there a
bus", the connection options and the loop-binding contract have one definition each.

Publishing runs inside signal handlers and request paths (sync); reading runs inside
an SSE response (async), so the two halves hold separate clients. Each is built
lazily, so importing a consumer never needs Redis or an event loop.
"""

from __future__ import annotations

from django.conf import settings

import redis
import redis.asyncio as aioredis

# Mirrors the cache pools' ``_REDIS_OPTIONS``. Connect is bounded on both clients; a read
# deadline is bounded on the *sync* one only, since the async readers block on purpose
# (``PubSub.get_message(timeout=20)``, the relay's ``xread(block=15000)``) and a socket
# read deadline shorter than the wait they ask for would cut it short. The async client
# therefore passes ``socket_timeout=None`` explicitly: redis-py 8.0 changed its default
# from ``None`` to 5s, which would interrupt those waits.
CONNECT_TIMEOUT_S = 5
SOCKET_TIMEOUT_S = 5


class RedisConnections:
    """The sync and async clients, built on first use and shared per process.

    An unconfigured ``DJANGO_REDIS_URL`` is a supported deployment shape, so callers ask
    ``configured`` and decide for themselves what to do about it: the UI event bus's
    publishers drop the poke silently, its readers raise.
    """

    def __init__(self) -> None:
        self._sync: redis.Redis | None = None
        self._async: aioredis.Redis | None = None

    @property
    def url(self) -> str:
        """A plain ``getattr``: the test settings leave the Redis component out of the
        include list entirely, so the setting can be absent rather than empty."""
        return getattr(settings, "DJANGO_REDIS_URL", "") or ""

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _url_or_raise(self) -> str:
        if not self.url:
            raise RuntimeError("DJANGO_REDIS_URL is not configured; this feature requires Redis.")
        return self.url

    def sync_client(self) -> redis.Redis:
        if self._sync is None:
            self._sync = self.build_sync_client()
        return self._sync

    def async_client(self) -> aioredis.Redis:
        """Web workers run a single event loop, so one shared pool is safe.

        ``redis.asyncio`` binds pooled connections to the loop that created them, so a
        caller on an ad-hoc loop (a management command's ``asyncio.run``, a fresh test
        loop) must build its own via ``build_async_client`` rather than share this one.
        """
        if self._async is None:
            self._async = self.build_async_client()
        return self._async

    def build_sync_client(self) -> redis.Redis:
        return redis.Redis.from_url(
            self._url_or_raise(),
            decode_responses=True,
            socket_connect_timeout=CONNECT_TIMEOUT_S,
            socket_timeout=SOCKET_TIMEOUT_S,
        )

    def build_async_client(self) -> aioredis.Redis:
        return aioredis.Redis.from_url(
            self._url_or_raise(),
            decode_responses=True,
            socket_connect_timeout=CONNECT_TIMEOUT_S,
            # redis-py 8.0 defaults ``socket_timeout`` to 5s, which would cut short the
            # blocking reads below; keep it unbounded to preserve the wait they ask for.
            socket_timeout=None,
        )


redis_connections = RedisConnections()
