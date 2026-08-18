"""Redis pub/sub bus for pushing dashboard shell state to connected browsers.

The nav shell (notification bell badge, "N running" sidebar badge) used to poll.
Now a single SSE connection per tab carries the updates, and this module is the
cross-process wire that wakes it: run transitions and notification writes happen
in the worker, the SSE readers live in the web process.

Two channels, split by who cares:

* ``daiv:ui-events:runs`` — broadcast. The running-runs badge is computed per
  viewer (``Run.objects.visible_to``), so resolving "who can see this run" at
  publish time would mean a query per connected user. Every reader recomputes
  its own count instead.
* ``daiv:ui-events:user:{id}`` — one recipient. Notifications are addressed.

Messages are pokes, not state: ``{"kind": "runs"}`` tells a reader *what* to
recompute, never the value. So a publisher never computes another user's count,
no payload can go stale in transit, and nothing sensitive crosses the bus.

Publishing is sync and fire-and-forget — it runs inside Django signal handlers
and request paths, where a Redis outage must degrade the badge, never fail the
write that triggered it. Subscribing is async, for the SSE readers. The two
halves therefore hold separate clients; each is created lazily so importing this
module never needs Redis (or an event loop).
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from django.conf import settings

import redis
import redis.asyncio as aioredis

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("daiv.core")

RUNS_CHANNEL = "daiv:ui-events:runs"

KIND_RUNS = "runs"
KIND_NOTIFICATIONS = "notifications"

_sync_client: redis.Redis | None = None
_async_client: aioredis.Redis | None = None


def user_channel(user_id: int | str) -> str:
    return f"daiv:ui-events:user:{user_id}"


def _redis_url() -> str:
    """The bus URL, or empty when there is no bus. A plain ``getattr`` because the test
    settings leave the Redis component out of the include list entirely."""
    return getattr(settings, "DJANGO_REDIS_URL", "") or ""


def _require_redis_url() -> str:
    url = _redis_url()
    if not url:
        raise RuntimeError("DJANGO_REDIS_URL is not configured; the UI event bus requires Redis.")
    return url


def get_sync_redis() -> redis.Redis:
    """Lazy process-wide client for publishers (signal handlers, request paths)."""
    global _sync_client  # noqa: PLW0603
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(_require_redis_url(), decode_responses=True)
    return _sync_client


def get_async_redis() -> aioredis.Redis:
    """Lazy process-wide client for SSE readers.

    Same loop-binding contract as ``chat.api.relay.get_redis``: ``redis.asyncio``
    binds pooled connections to the loop that created them, so this must only be
    used from the web worker's event loop.
    """
    global _async_client  # noqa: PLW0603
    if _async_client is None:
        _async_client = aioredis.Redis.from_url(_require_redis_url(), decode_responses=True)
    return _async_client


def _message(kind: str) -> str:
    return json.dumps({"kind": kind})


def _on_publish_failure(channel: str, kind: str, err: Exception) -> None:
    """A dropped poke costs at most one badge refresh cycle — readers resync on
    reconnect — so this never surfaces into the write that triggered it. WARNING without
    a traceback: a Redis outage is an anticipated external failure, and one Sentry error
    event per run transition would bury the real ones."""
    logger.warning("ui_events: failed to publish %s on %s: %s", kind, channel, err)


def _bus_configured() -> bool:
    """Whether there is a bus to publish onto.

    Publishers drop the poke silently when there isn't: the badges then keep their
    page-load values, as they did before the stream existed. Nothing is logged on this
    path — it would fire on every run transition and notification write, burying the real
    failures; a missing Redis surfaces loudly on the reader side, which raises.
    """
    return bool(_redis_url())


def _publish(channel: str, kind: str) -> None:
    if not _bus_configured():
        return
    try:
        get_sync_redis().publish(channel, _message(kind))
    except Exception as err:  # noqa: BLE001
        _on_publish_failure(channel, kind, err)


async def _apublish(channel: str, kind: str) -> None:
    """``_publish`` for callers already on the web worker's event loop, sharing its
    guard and failure policy. Same loop-binding contract as ``get_async_redis``."""
    if not _bus_configured():
        return
    try:
        await get_async_redis().publish(channel, _message(kind))
    except Exception as err:  # noqa: BLE001
        _on_publish_failure(channel, kind, err)


def publish_runs_changed() -> None:
    """Poke every reader to recompute its visible running-runs count.

    Fire-and-forget, and *not* commit-deferred: keeping ``transaction.on_commit`` at the
    call sites is what lets this stay usable without a DB connection (and lets tests
    patch the publisher without patching the deferral out of the code under test). Sync
    callers must wrap it — see ``sessions.signals.publish_nav_runs_changed``.
    """
    _publish(RUNS_CHANNEL, KIND_RUNS)


async def apublish_runs_changed() -> None:
    """``publish_runs_changed`` for callers already on the web worker's event loop.

    The chat run finalizer lives there and would otherwise pay a thread hop for a
    single round-trip. No ``on_commit``: it runs outside the ORM's sync transaction
    machinery, and its own write has already been awaited.
    """
    await _apublish(RUNS_CHANNEL, KIND_RUNS)


def publish_notifications_changed(user_id: int | str | None) -> None:
    """Poke one user's readers to recompute their unread count.

    Commit-deferral is the caller's, on the same terms as ``publish_runs_changed``.
    """
    if user_id is None:
        return
    _publish(user_channel(user_id), KIND_NOTIFICATIONS)


@asynccontextmanager
async def subscription(*channels: str) -> AsyncIterator[aioredis.client.PubSub]:
    """Subscribe to ``channels`` for the lifetime of the block.

    ``aclose`` unsubscribes *and* returns the connection to the pool; leaving that
    to garbage collection would leak one Redis connection per closed SSE stream.
    """
    pubsub = get_async_redis().pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(*channels)
        yield pubsub
    finally:
        await pubsub.aclose()


def parse_kind(message: dict | None) -> str | None:
    """Read the poke kind out of a raw pub/sub message, or ``None`` if unreadable.

    Tolerates malformed payloads (a stray ``PUBLISH`` from redis-cli, a future
    sender with a different shape) because a reader that raises here drops an SSE
    connection the browser then has to rebuild.
    """
    if not message or message.get("type") != "message":
        return None
    try:
        return json.loads(message["data"]).get("kind")
    except TypeError, ValueError, KeyError, AttributeError:
        logger.warning("ui_events: unreadable message on %s", message.get("channel"))
        return None
