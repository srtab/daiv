from contextlib import asynccontextmanager

from django.conf import settings

from langgraph.checkpoint.redis.aio import AsyncRedisSaver


@asynccontextmanager
async def open_checkpointer():
    """Yield a configured AsyncRedisSaver using project settings.

    Single source of truth for the Redis connection + TTL used by the chat endpoint,
    the job task, and the chat dashboard views.
    """
    async with AsyncRedisSaver.from_conn_string(
        settings.DJANGO_REDIS_CHECKPOINT_URL, ttl={"default_ttl": settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES}
    ) as cp:
        yield cp
