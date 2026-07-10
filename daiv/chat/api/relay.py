"""Redis-Streams relay for chat run events.

The chat run executor (``chat.api.runner``) publishes every AG-UI event here;
SSE readers (``chat.api.views``) replay + tail the stream so a browser can
rejoin an in-flight run after a refresh or connection drop.

Contract:

* Stream key ``daiv:chat:run-events:{thread_id}:{run_id}`` — the thread id is
  embedded so reader authorization reduces to thread visibility.
* Normal entries: ``{"data": <AG-UI event JSON (by_alias, exclude_none)>}``.
* Terminal sentinel: ``{"end": "1"}`` — always published by the runner's
  ``finally``, so readers can distinguish "run finished" from "writer died".
* Cancel flag ``daiv:chat:run-cancel:{thread_id}:{run_id}`` — set by the
  cancel endpoint, polled by ``ChatRunStreamer`` at heartbeat cadence.

This module is pure Redis: no ORM, no view logic. All helpers accept an
explicit ``client`` for tests; production callers rely on the lazy singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from redis.asyncio import Redis

# ~1h of retention after the last publish; MAXLEN caps runaway runs. A chat
# turn emits well under 10k events, so replay-from-zero is always complete.
RUN_EVENTS_MAXLEN = 10_000
RUN_EVENTS_TTL_S = 3600
CANCEL_TTL_S = 3600

DATA_FIELD = "data"
END_FIELD = "end"

_client: Redis | None = None


def _build_client() -> Redis:
    if not settings.DJANGO_REDIS_URL:
        raise RuntimeError("DJANGO_REDIS_URL is not configured; the chat event relay requires Redis.")
    return aioredis.Redis.from_url(settings.DJANGO_REDIS_URL, decode_responses=True)


def get_redis() -> Redis:
    """Lazy process-wide client. Web workers run a single event loop, so one
    shared connection pool is safe; tests patch this function instead."""
    global _client  # noqa: PLW0603
    if _client is None:
        _client = _build_client()
    return _client


def run_events_key(thread_id: str, run_id: str) -> str:
    return f"daiv:chat:run-events:{thread_id}:{run_id}"


def cancel_key(thread_id: str, run_id: str) -> str:
    return f"daiv:chat:run-cancel:{thread_id}:{run_id}"


async def publish_event(thread_id: str, run_id: str, data: str, *, client: Redis | None = None) -> None:
    r = client or get_redis()
    key = run_events_key(thread_id, run_id)
    await r.xadd(key, {DATA_FIELD: data}, maxlen=RUN_EVENTS_MAXLEN, approximate=True)
    await r.expire(key, RUN_EVENTS_TTL_S)


async def publish_end(thread_id: str, run_id: str, *, client: Redis | None = None) -> None:
    r = client or get_redis()
    key = run_events_key(thread_id, run_id)
    await r.xadd(key, {END_FIELD: "1"}, maxlen=RUN_EVENTS_MAXLEN, approximate=True)
    await r.expire(key, RUN_EVENTS_TTL_S)


async def request_cancel(thread_id: str, run_id: str, *, client: Redis | None = None) -> None:
    r = client or get_redis()
    await r.set(cancel_key(thread_id, run_id), "1", ex=CANCEL_TTL_S)


async def cancel_requested(thread_id: str, run_id: str, *, client: Redis | None = None) -> bool:
    r = client or get_redis()
    return bool(await r.get(cancel_key(thread_id, run_id)))
