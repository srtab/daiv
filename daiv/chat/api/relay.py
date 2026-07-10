"""Redis-Streams relay for chat run events.

The chat run executor (``chat.api.runner``) publishes every AG-UI event here;
SSE readers (``chat.api.views``) replay + tail the stream so a browser can
rejoin an in-flight run after a refresh or connection drop.

Contract:

* Stream key ``daiv:chat:run-events:{thread_id}:{run_id}`` — the thread id is
  embedded so reader authorization reduces to thread visibility.
* Normal entries: ``{"data": <AG-UI event JSON (by_alias, exclude_none)>}``.
* Terminal sentinel: ``{"end": "1"}`` — published on a best-effort basis by the
  runner's ``finally`` (if Redis is down even that can fail, in which case readers
  fall back to the liveness probe in ``_run_event_frames``), so readers can
  usually distinguish "run finished" from "writer died".
* Cancel flag ``daiv:chat:run-cancel:{thread_id}:{run_id}`` — set by the cancel
  endpoint, checked by ``ChatRunStreamer`` at the next event boundary once the
  heartbeat interval elapses (a stalled, event-less run won't observe it until it
  emits again; the local ``asyncio.Task`` cancel is what stops such a run promptly).

Organization: a run's relay state (its event stream + cancel flag) is a single
``RunRelay`` object bound to ``(thread_id, run_id)`` — every operation for one run
lives there, and the Redis wire format (field names, sentinel convention, ``xread``
shape) is its private concern. Process-wide connection lifecycle is a separate,
module-level concern (``get_redis`` / the lazy singleton); ``RunRelay`` accepts an
explicit ``client`` for tests and otherwise resolves the singleton lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.conf import settings

import redis.asyncio as aioredis

if TYPE_CHECKING:
    from redis.asyncio import Redis

_client: Redis | None = None


def _build_client() -> Redis:
    if not settings.DJANGO_REDIS_URL:
        raise RuntimeError("DJANGO_REDIS_URL is not configured; the chat event relay requires Redis.")
    return aioredis.Redis.from_url(settings.DJANGO_REDIS_URL, decode_responses=True)


def get_redis() -> Redis:
    """Lazy process-wide client. Web workers run a single event loop, so one
    shared connection pool is safe; tests patch this function instead.

    Must only be used from the web-worker event loop: ``redis.asyncio`` binds
    pooled connections to the loop that created them, so reusing this singleton
    from an ad-hoc loop (a management command's ``asyncio.run(...)``, a fresh
    test loop) would raise ``RuntimeError: got Future attached to a different
    loop``. Such callers should build their own client via ``_build_client``.
    """
    global _client  # noqa: PLW0603
    if _client is None:
        _client = _build_client()
    return _client


class StreamEntry(NamedTuple):
    """One parsed relay entry. ``is_end`` flags the terminal sentinel; ``data``
    is the AG-UI event JSON for normal entries (``None`` for the sentinel)."""

    id: str
    is_end: bool
    data: str | None


class RunRelay:
    """Relay operations for a single chat run, bound to ``(thread_id, run_id)``.

    Holds the run's event stream (publish + tail) and its cancel flag behind one
    object so a caller deals in ``RunRelay(thread_id, run_id).publish_event(...)``
    rather than threading the id pair through every call. The Redis wire format
    lives here; consumers of ``read_events`` see only ``StreamEntry`` values.

    ``client`` is injected by tests; production callers omit it and share the
    lazy process-wide singleton (resolved on each use, so an instance can be
    built off the web-worker loop and used on it — see ``get_redis``).
    """

    # ~1h of retention after the last publish; MAXLEN caps runaway runs. This assumes
    # a chat turn stays well under ``EVENTS_MAXLEN`` — if that stops holding, MAXLEN
    # trimming would drop the head of a long run and replay-from-zero would start
    # mid-stream (leaving the client unable to render an orphaned tail). No test
    # enforces the margin, so revisit the ceiling if per-turn event volume grows.
    EVENTS_MAXLEN = 10_000
    EVENTS_TTL_S = 3600
    CANCEL_TTL_S = 3600

    DATA_FIELD = "data"
    END_FIELD = "end"

    def __init__(self, thread_id: str, run_id: str, *, client: Redis | None = None) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self._client = client

    @property
    def _redis(self) -> Redis:
        return self._client or get_redis()

    @property
    def events_key(self) -> str:
        return f"daiv:chat:run-events:{self.thread_id}:{self.run_id}"

    @property
    def cancel_key(self) -> str:
        return f"daiv:chat:run-cancel:{self.thread_id}:{self.run_id}"

    async def _append(self, fields: dict[str, str]) -> None:
        """Append one entry to the run's stream and refresh its retention TTL.

        XADD + EXPIRE are pipelined into a single round-trip: this runs once per
        published event — on the per-token streaming path — so issuing the EXPIRE as
        a separate call would double the relay's per-event Redis latency.
        """
        key = self.events_key
        async with self._redis.pipeline(transaction=False) as pipe:
            # ty: redis' ``xadd`` stub types ``fields`` as an invariant ``Dict[FieldT, EncodableT]``,
            # so a ``dict[str, str]`` variable (unlike an inline literal) is rejected — a stub gap, not a real mismatch.
            pipe.xadd(key, fields, maxlen=self.EVENTS_MAXLEN, approximate=True)  # ty: ignore[invalid-argument-type]
            pipe.expire(key, self.EVENTS_TTL_S)
            await pipe.execute()

    async def publish_event(self, data: str) -> None:
        await self._append({self.DATA_FIELD: data})

    async def publish_end(self) -> None:
        await self._append({self.END_FIELD: "1"})

    async def read_events(self, last_id: str, *, block_ms: int, count: int = 100) -> list[StreamEntry]:
        """Block-read the next batch of entries after ``last_id``, parsed.

        The read counterpart to ``publish_*``: keeps the stream's wire format (field
        names, sentinel convention, ``xread`` shape) inside this class so SSE readers
        deal only in ``StreamEntry`` values. An empty list means the blocking read
        timed out with nothing new.
        """
        key = self.events_key
        entries = await self._redis.xread({key: last_id}, count=count, block=block_ms)
        if not entries:
            return []
        return [
            StreamEntry(id=entry_id, is_end=self.END_FIELD in fields, data=fields.get(self.DATA_FIELD))
            for entry_id, fields in entries[0][1]
        ]

    async def request_cancel(self) -> None:
        await self._redis.set(self.cancel_key, "1", ex=self.CANCEL_TTL_S)

    async def cancel_requested(self) -> bool:
        return bool(await self._redis.get(self.cancel_key))
