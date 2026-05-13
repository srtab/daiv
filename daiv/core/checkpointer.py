from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from django.conf import settings

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

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
