from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from django.conf import settings

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.store.redis.aio import AsyncRedisStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[AsyncRedisSaver]:
    """Yield a configured AsyncRedisSaver using project settings.

    Single source of truth for the Redis connection + TTL.
    """
    async with AsyncRedisSaver.from_conn_string(
        settings.DJANGO_REDIS_CHECKPOINT_URL, ttl={"default_ttl": settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES}
    ) as cp:
        yield cp


@asynccontextmanager
async def open_store() -> AsyncIterator[AsyncRedisStore]:
    """Yield a configured AsyncRedisStore using project settings.

    Backs the agent filesystem in repoless mode (``DAIVStoreBackend``) so files written
    in one turn are visible in the next turn of the same ``thread_id``. Shares the
    checkpointer's Redis URL and TTL so message history and FS state expire together.
    """
    async with AsyncRedisStore.from_conn_string(
        settings.DJANGO_REDIS_CHECKPOINT_URL, ttl={"default_ttl": settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES}
    ) as store:
        yield store
