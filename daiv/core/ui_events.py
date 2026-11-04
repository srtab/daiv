"""Redis pub/sub bus for pushing dashboard shell state to connected browsers.

The nav shell's live badges (unread count, "N running") are fed by one SSE connection
per tab, and this module is the cross-process wire that wakes it: run transitions and
notification writes happen in the worker, the SSE readers live in the web process.

Four objects, split by concern:

* ``Channel`` — addressing. ``daiv:ui-events:runs`` is a broadcast because the
  running-runs badge is per viewer (``Run.objects.visible_to``), so resolving "who can
  see this run" at publish time would mean a query per connected user; every reader
  recomputes its own count instead. ``daiv:ui-events:user:{id}`` has one recipient,
  because notifications are addressed. ``daiv:ui-events:session:{thread_id}`` is joined
  only by a viewer of that session's page, and each poke on it also bumps the
  ``session-rev`` counter beside it: pub/sub forgets a poke nobody was attached for, so
  a page that (re)attaches compares the counter with the one it last rendered
  (``read_session_revision``) to learn whether it missed one.
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

Connection lifecycle is not one of them: the clients come from ``core.redis``, shared
with the chat relay. Publishing is sync because it runs inside signal handlers and
request paths, reading is async because it runs inside an SSE response, so the two
halves hold separate clients.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from time import monotonic
from typing import TYPE_CHECKING, Self

from django.conf import settings

import redis

from core.redis import RedisConnections, redis_connections

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

logger = logging.getLogger("daiv.core")


def is_transient_bus_error(exc: BaseException) -> bool:
    """True for an anticipated bus outage rather than a bug in how we use it.

    Readers log a dropped bus at WARNING without a traceback and reconnect on a back-off,
    so one Sentry error per tab per retry would bury the real failures. ``RedisError`` covers the
    client's own failures, ``OSError`` the socket beneath it (builtin ``ConnectionError``
    and ``TimeoutError`` are both ``OSError``). Both are ``Exception`` subclasses, so
    ``CancelledError`` is never classified here and keeps propagating.
    """
    return isinstance(exc, redis.RedisError | OSError)


class Channel:
    """Where a poke is addressed. Both halves of the bus name channels through here."""

    RUNS = "daiv:ui-events:runs"

    @staticmethod
    def for_user(user_id: int | str) -> str:
        return f"daiv:ui-events:user:{user_id}"

    @staticmethod
    def for_session(thread_id: str) -> str:
        return f"daiv:ui-events:session:{thread_id}"

    @staticmethod
    def session_revision(thread_id: str) -> str:
        return f"daiv:ui-events:session-rev:{thread_id}"


class UIEventKind(StrEnum):
    """What a poke says changed, and the wire format that carries it.

    A ``StrEnum`` because the member *is* the wire token — hence ``as_payload``/
    ``from_message`` rather than ``encode``/``decode``, which would shadow ``str``'s own.
    """

    RUNS = "runs"
    NOTIFICATIONS = "notifications"
    SESSION = "session"

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

    def notifications_changed(self, user_id: int | str | None) -> None:
        """Poke one user's readers to recompute their unread count."""
        if user_id is None:
            return
        self._publish(Channel.for_user(user_id), UIEventKind.NOTIFICATIONS)

    def session_changed(self, thread_id: str) -> None:
        """Bump a session's revision, then poke the viewers of its page.

        One pipeline, so the counter and the poke can't be separated by a failure between
        them; the counter moves first so a reader woken by the poke never sees a stale one.
        """
        if not self._connections.configured:
            return
        channel = Channel.for_session(thread_id)
        try:
            revision_key = Channel.session_revision(thread_id)
            pipe = self._connections.sync_client().pipeline()
            pipe.incr(revision_key)
            pipe.expire(revision_key, settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES * 60)
            pipe.publish(channel, UIEventKind.SESSION.as_payload())
            pipe.execute()
        except Exception as err:  # noqa: BLE001
            self._log_failure(channel, UIEventKind.SESSION, err)

    def _publish(self, channel: str, kind: UIEventKind) -> None:
        if not self._connections.configured:
            return
        try:
            self._connections.sync_client().publish(channel, kind.as_payload())
        except Exception as err:  # noqa: BLE001
            self._log_failure(channel, kind, err)

    def _log_failure(self, channel: str, kind: UIEventKind, err: Exception) -> None:
        """WARNING without a traceback: a Redis outage is an anticipated external failure,
        and one Sentry error event per run transition would bury the real ones. An
        unconfigured bus logs nothing at all — that check fires on every write, and the
        misconfiguration surfaces loudly on the reader side, which raises."""
        logger.warning("ui_events: failed to publish %s on %s: %s", kind, channel, err)


publisher = UIEventPublisher()


def read_session_revision(thread_id: str, *, connections: RedisConnections | None = None) -> int:
    """A session's revision, or ``0`` when there is none to read.

    Missing key, unconfigured bus and a Redis outage all read as ``0``: callers compare
    for *difference*, so a revision that vanished or went backwards still makes a page fetch.
    """
    connections = connections or redis_connections
    if not connections.configured:
        return 0
    try:
        return _parse_revision(connections.sync_client().get(Channel.session_revision(thread_id)))
    except (redis.RedisError, OSError) as err:
        logger.warning("ui_events: failed to read the revision of session %s: %s", thread_id, err)
        return 0


async def aread_session_revision(thread_id: str, *, connections: RedisConnections | None = None) -> int:
    """``read_session_revision`` for async callers."""
    connections = connections or redis_connections
    if not connections.configured:
        return 0
    try:
        return _parse_revision(await connections.async_client().get(Channel.session_revision(thread_id)))
    except (redis.RedisError, OSError) as err:
        logger.warning("ui_events: failed to read the revision of session %s: %s", thread_id, err)
        return 0


def _parse_revision(raw: bytes | str | None) -> int:
    return int(raw) if raw is not None else 0


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
    # Ceiling on that absorb. Without it a deployment poking faster than COALESCE_S feeds
    # the loop forever, and the reader burns its whole duration cap without recounting.
    ABSORB_MAX_S = 1.0

    def __init__(self, *channels: str, connections: RedisConnections | None = None) -> None:
        self._channels = channels
        self._connections = connections or redis_connections
        self._pubsub: PubSub | None = None

    @classmethod
    def for_user(
        cls, user_id: int | str, *, session: str | None = None, connections: RedisConnections | None = None
    ) -> Self:
        """The channels one viewer's dashboard shell needs: the broadcast run channel,
        plus the one their own notifications are addressed to, plus the session's own when
        the viewer has that session's page open."""
        channels = [Channel.RUNS, Channel.for_user(user_id)]
        if session is not None:
            channels.append(Channel.for_session(session))
        return cls(*channels, connections=connections)

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

    async def wait_for_change(self, timeout: float) -> frozenset[UIEventKind]:
        """Wait up to ``timeout`` for a poke, absorb the rest of its burst, and report
        every kind the burst carried — empty when nothing readable arrived.

        An unreadable message counts as nothing arriving, so it never opens a burst; one
        inside a burst is absorbed without contributing a kind.
        """
        first = UIEventKind.from_message(await self._next_message(timeout))
        if first is None:
            return frozenset()
        kinds = {first}
        deadline = monotonic() + self.ABSORB_MAX_S
        while monotonic() < deadline and (message := await self._next_message(self.COALESCE_S)) is not None:
            if (kind := UIEventKind.from_message(message)) is not None:
                kinds.add(kind)
        return frozenset(kinds)

    async def _next_message(self, timeout: float) -> dict | None:
        """``None`` is how ``redis.asyncio`` reports "nothing within the timeout"."""
        if self._pubsub is None:
            raise RuntimeError("UIEventStream is not subscribed; enter it as an async context manager first.")
        return await self._pubsub.get_message(timeout=timeout)
