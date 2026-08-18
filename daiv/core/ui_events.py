"""Redis pub/sub bus for pushing dashboard shell state to connected browsers.

The nav shell (notification bell badge, "N running" sidebar badge) used to poll. Now
a single SSE connection per tab carries the updates, and this module is the
cross-process wire that wakes it: run transitions and notification writes happen in
the worker, the SSE readers live in the web process.

Four objects, split by concern:

* ``Channel`` — addressing. ``daiv:ui-events:runs`` is a broadcast because the
  running-runs badge is per viewer (``Run.objects.visible_to``), so resolving "who can
  see this run" at publish time would mean a query per connected user; every reader
  recomputes its own count instead. ``daiv:ui-events:user:{id}`` has one recipient,
  because notifications are addressed.
* ``UIEventKind`` — the vocabulary *and* the wire format that carries it, so the two
  ends of the wire can't drift apart. Messages are pokes, not state: ``{"kind": "runs"}``
  tells a reader what to recompute, never the value. So a publisher never computes
  another user's count, no payload can go stale in transit, and nothing sensitive
  crosses the bus.
* ``UIEventPublisher`` (module singleton ``publisher``) — the write side and its
  fire-and-forget policy.
* ``UIEventStream`` — the read side: which channels a viewer needs, how a burst of
  pokes is absorbed, and what to make of a malformed message. Callers see
  ``wait_for_change()``, never a raw pub/sub message.

Connection lifecycle is separate from all four (``RedisConnections``, module singleton
``redis_connections``), as in ``chat.api.relay``: publishing is sync because it runs
inside signal handlers and request paths, reading is async because it runs inside an
SSE response, so the two halves hold separate clients. Each is built lazily, so
importing this module never needs Redis (or an event loop).
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from django.conf import settings

import redis
import redis.asyncio as aioredis

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

logger = logging.getLogger("daiv.core")


class Channel:
    """Where a poke is addressed. Both halves of the bus name channels through here."""

    RUNS = "daiv:ui-events:runs"

    @staticmethod
    def for_user(user_id: int | str) -> str:
        return f"daiv:ui-events:user:{user_id}"


class UIEventKind(StrEnum):
    """What a poke says changed, and the wire format that carries it.

    A ``StrEnum`` because the member *is* the wire token — hence ``as_payload``/
    ``from_message`` rather than ``encode``/``decode``, which would shadow ``str``'s own.
    """

    RUNS = "runs"
    NOTIFICATIONS = "notifications"

    def as_payload(self) -> str:
        return json.dumps({"kind": self.value})

    @classmethod
    def from_message(cls, message: dict | None) -> Self | None:
        """Read the kind off a raw pub/sub message, or ``None`` if there isn't one.

        Tolerates malformed payloads (a stray ``PUBLISH`` from redis-cli, a future sender
        with a shape or a kind this process doesn't know) because a reader that raises
        here drops an SSE connection the browser then has to rebuild.
        """
        if not message or message.get("type") != "message":
            return None
        try:
            return cls(json.loads(message["data"])["kind"])
        except TypeError, ValueError, KeyError, AttributeError:
            logger.warning("ui_events: unreadable message on %s", message.get("channel"))
            return None


class RedisConnections:
    """Lazy, process-wide Redis clients for the bus.

    An unconfigured ``DJANGO_REDIS_URL`` is a supported deployment shape, so the two
    halves ask ``configured`` and decide for themselves what to do about it: publishers
    drop the poke silently, readers raise.
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
            raise RuntimeError("DJANGO_REDIS_URL is not configured; the UI event bus requires Redis.")
        return self.url

    def sync_client(self) -> redis.Redis:
        if self._sync is None:
            self._sync = redis.Redis.from_url(self._url_or_raise(), decode_responses=True)
        return self._sync

    def async_client(self) -> aioredis.Redis:
        """Same loop-binding contract as ``chat.api.relay.get_redis``: ``redis.asyncio``
        binds pooled connections to the loop that created them, so this must only be used
        from the web worker's event loop."""
        if self._async is None:
            self._async = aioredis.Redis.from_url(self._url_or_raise(), decode_responses=True)
        return self._async


redis_connections = RedisConnections()


class UIEventPublisher:
    """Puts pokes on the bus, and never lets a failure doing so reach its caller.

    Publishers sit inside signal handlers and request paths, where a Redis outage must
    degrade the badge and not fail the write that triggered it. A dropped poke costs at
    most one refresh cycle — readers resync on their next reconnect.

    Publishing is *not* commit-deferred here: keeping ``transaction.on_commit`` at the
    call sites is what lets this stay usable without a DB connection, and lets a test
    patch a publisher without patching the deferral out of the code under test. Every
    sync caller must wrap — see ``sessions.signals.publish_nav_runs_changed``.
    """

    def __init__(self, connections: RedisConnections | None = None) -> None:
        self._connections = connections or redis_connections

    def runs_changed(self) -> None:
        """Poke every reader to recompute its visible running-runs count."""
        self._publish(Channel.RUNS, UIEventKind.RUNS)

    async def aruns_changed(self) -> None:
        """``runs_changed`` for callers already on the web worker's event loop.

        The chat run finalizer lives there and would otherwise pay a thread hop for a
        single round-trip. No ``on_commit`` for that one either: it runs outside the ORM's
        sync transaction machinery, and its own write has already been awaited.
        """
        await self._apublish(Channel.RUNS, UIEventKind.RUNS)

    def notifications_changed(self, user_id: int | str | None) -> None:
        """Poke one user's readers to recompute their unread count."""
        if user_id is None:
            return
        self._publish(Channel.for_user(user_id), UIEventKind.NOTIFICATIONS)

    def _publish(self, channel: str, kind: UIEventKind) -> None:
        if not self._connections.configured:
            return
        try:
            self._connections.sync_client().publish(channel, kind.as_payload())
        except Exception as err:  # noqa: BLE001
            self._log_failure(channel, kind, err)

    async def _apublish(self, channel: str, kind: UIEventKind) -> None:
        if not self._connections.configured:
            return
        try:
            await self._connections.async_client().publish(channel, kind.as_payload())
        except Exception as err:  # noqa: BLE001
            self._log_failure(channel, kind, err)

    def _log_failure(self, channel: str, kind: UIEventKind, err: Exception) -> None:
        """WARNING without a traceback: a Redis outage is an anticipated external failure,
        and one Sentry error event per run transition would bury the real ones. An
        unconfigured bus logs nothing at all — that check fires on every write, and the
        misconfiguration surfaces loudly on the reader side, which raises."""
        logger.warning("ui_events: failed to publish %s on %s: %s", kind, channel, err)


publisher = UIEventPublisher()


class UIEventStream:
    """One reader's subscription to the pokes it cares about.

    Used as an async context manager: ``aclose`` unsubscribes *and* returns the
    connection to the pool, so leaving that to garbage collection would leak one Redis
    connection per closed SSE stream.

    Callers deal in ``wait_for_change()`` — the channel names, the burst coalescing and
    the junk tolerance are this class's concern, not the SSE handler's.
    """

    # A finished run pokes several times over (the status write, then the dispatcher's
    # follow-ups). Absorbing the burst here is what turns those into one recompute.
    COALESCE_S = 0.15

    def __init__(self, *channels: str, connections: RedisConnections | None = None) -> None:
        self._channels = channels
        self._connections = connections or redis_connections
        self._pubsub: PubSub | None = None

    @classmethod
    def for_user(cls, user_id: int | str, *, connections: RedisConnections | None = None) -> Self:
        """The channels one viewer's dashboard shell needs: the broadcast run channel,
        plus the one their own notifications are addressed to."""
        return cls(Channel.RUNS, Channel.for_user(user_id), connections=connections)

    async def __aenter__(self) -> Self:
        self._pubsub = self._connections.async_client().pubsub(ignore_subscribe_messages=True)
        try:
            await self._pubsub.subscribe(*self._channels)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._pubsub is not None:
            pubsub, self._pubsub = self._pubsub, None
            await pubsub.aclose()

    async def wait_for_change(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for a poke, absorb the rest of its burst, and report
        whether anything arrived.

        Which kind arrived is deliberately not reported: one recompute covers every
        counter, which also means a poke of either kind repairs a badge whose own poke was
        dropped. An unreadable message counts as nothing arriving.
        """
        if UIEventKind.from_message(await self._next_message(timeout)) is None:
            return False
        while await self._next_message(self.COALESCE_S) is not None:
            pass
        return True

    async def _next_message(self, timeout: float) -> dict | None:
        """``None`` is how ``redis.asyncio`` reports "nothing within the timeout"."""
        if self._pubsub is None:
            raise RuntimeError("UIEventStream is not subscribed; enter it as an async context manager first.")
        return await self._pubsub.get_message(timeout=timeout)
