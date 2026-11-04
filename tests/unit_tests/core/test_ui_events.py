"""Wire-level tests for the nav event bus.

The bus carries pokes between processes (a worker finishing a run, the web worker
holding an SSE stream), so both halves of the contract are pinned here: what a
publisher puts on the wire, and what a reader makes of it — including the junk cases,
since a reader that raises drops a browser's stream.

Connections are injected rather than patched: every object on the bus takes a
``RedisConnections``, so a test builds its own instead of reaching into the module's
singletons (which would leak a cached client into whatever runs next).
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest
import redis

from core.ui_events import (
    Channel,
    UIEventKind,
    UIEventPublisher,
    UIEventStream,
    aread_session_revision,
    is_transient_bus_error,
)


class FakeConnections:
    """A bus whose client is whatever the test wants to observe."""

    def __init__(self, client: Mock | None = None, *, configured: bool = True):
        self.client = client or Mock()
        self.configured = configured

    def sync_client(self) -> Mock:
        return self.client

    def async_client(self) -> Mock:
        return self.client


class FakePubSub:
    """Replays a scripted burst of pokes, then reports timeouts forever.

    ``None`` is how ``redis.asyncio`` signals "nothing within the timeout".
    """

    def __init__(self, messages: list[dict | None] | None = None):
        self.messages = list(messages or [])
        self.channels: tuple[str, ...] = ()
        self.reads: list[float] = []
        self.closed = False

    async def subscribe(self, *channels):
        self.channels = channels

    async def get_message(self, timeout=None):
        self.reads.append(timeout)
        return self.messages.pop(0) if self.messages else None

    async def aclose(self):
        self.closed = True


def poke(kind: str = "runs", channel: str = Channel.RUNS) -> dict:
    return {"type": "message", "channel": channel, "data": json.dumps({"kind": kind})}


def publisher_with(client: Mock, *, configured: bool = True) -> UIEventPublisher:
    return UIEventPublisher(connections=FakeConnections(client, configured=configured))


def stream_on(pubsub: FakePubSub, *channels: str) -> UIEventStream:
    connections = FakeConnections(Mock(pubsub=Mock(return_value=pubsub)))
    return UIEventStream(*(channels or ("a",)), connections=connections)


class TestChannel:
    def test_runs_are_broadcast_and_notifications_are_addressed(self):
        """Resolving "who can see this run" at publish time would mean a query per
        connected user, so run pokes go to everyone and readers filter for themselves."""
        assert Channel.RUNS == "daiv:ui-events:runs"
        assert Channel.for_user(42) == "daiv:ui-events:user:42"

    def test_a_session_has_its_own_channel_and_revision_key(self):
        assert Channel.for_session("t1") == "daiv:ui-events:session:t1"
        assert Channel.session_revision("t1") == "daiv:ui-events:session-rev:t1"


class TestWireFormat:
    def test_a_poke_carries_no_state(self):
        """Only a kind crosses the bus — readers recompute. A count in the payload would
        mean publishers computing other users' visibility."""
        assert json.loads(UIEventKind.RUNS.as_payload()) == {"kind": "runs"}

    def test_what_a_publisher_writes_is_what_a_reader_reads(self):
        encoded = UIEventKind.NOTIFICATIONS.as_payload()
        message = {"type": "message", "channel": "c", "data": encoded}
        assert UIEventKind.from_message(message) is UIEventKind.NOTIFICATIONS

    @pytest.mark.parametrize(
        "message",
        [
            None,
            {"type": "subscribe", "channel": "c", "data": 1},
            {"type": "message", "channel": "c", "data": "not json"},
            {"type": "message", "channel": "c", "data": "[]"},
            {"type": "message", "channel": "c", "data": json.dumps({"kind": "from-the-future"})},
            {"type": "message", "channel": "c"},
        ],
        ids=["timeout", "subscribe-ack", "not-json", "wrong-shape", "unknown-kind", "no-data"],
    )
    def test_junk_reads_as_nothing_rather_than_raising(self, message):
        # A raise here would abort the SSE response mid-stream, which the browser can
        # only read as a dropped connection.
        assert UIEventKind.from_message(message) is None


class TestPublish:
    def test_runs_changed_goes_to_the_broadcast_channel(self):
        client = Mock()
        publisher_with(client).runs_changed()
        client.publish.assert_called_once_with(Channel.RUNS, UIEventKind.RUNS.as_payload())

    def test_notifications_changed_goes_to_that_user_alone(self):
        client = Mock()
        publisher_with(client).notifications_changed(42)
        client.publish.assert_called_once_with("daiv:ui-events:user:42", UIEventKind.NOTIFICATIONS.as_payload())

    def test_session_changed_bumps_the_revision_then_pokes_the_session_channel(self, settings):
        settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES = 90
        pipe = Mock()
        client = Mock(pipeline=Mock(return_value=pipe))
        assert publisher_with(client).session_changed("t1") is True
        pipe.incr.assert_called_once_with("daiv:ui-events:session-rev:t1")
        pipe.expire.assert_called_once_with("daiv:ui-events:session-rev:t1", 90 * 60)
        pipe.publish.assert_called_once_with("daiv:ui-events:session:t1", '{"kind": "session"}')
        pipe.execute.assert_called_once_with()
        client.publish.assert_not_called()

    def test_session_changed_swallows_a_redis_failure(self, settings, caplog):
        settings.DJANGO_REDIS_CHECKPOINT_TTL_MINUTES = 90
        pipe = Mock(execute=Mock(side_effect=redis.ConnectionError("redis is down")))
        with caplog.at_level(logging.WARNING, logger="daiv.core"):
            assert publisher_with(Mock(pipeline=Mock(return_value=pipe))).session_changed("t1") is False
        assert "failed to publish" in caplog.text

    def test_session_changed_on_an_unconfigured_bus_is_a_silent_no_op(self, caplog):
        client = Mock()
        assert publisher_with(client, configured=False).session_changed("t1") is False
        client.pipeline.assert_not_called()
        assert "failed to publish" not in caplog.text

    def test_no_recipient_is_a_no_op(self):
        client = Mock()
        publisher_with(client).notifications_changed(None)
        client.publish.assert_not_called()

    def test_a_redis_failure_never_reaches_the_caller(self, caplog):
        """Publishers sit inside signal handlers: a Redis outage must cost a badge
        refresh, never the write that triggered it."""
        client = Mock(publish=Mock(side_effect=ConnectionError("redis is down")))
        publisher_with(client).runs_changed()
        assert "failed to publish" in caplog.text

    def test_an_unconfigured_bus_drops_the_poke_without_a_warning(self, caplog):
        """Deployments (and the test settings) can omit Redis entirely. That is not an
        outage, so it must not log one per write — the badges simply stop being live, and
        the reader side is where the misconfiguration surfaces."""
        client = Mock()
        publisher_with(client, configured=False).runs_changed()
        client.publish.assert_not_called()
        assert "failed to publish" not in caplog.text


class TestRevision:
    """A tab that was disconnected when a poke went out learns it missed one by comparing
    revisions, so every failure reads as ``0`` (the page then fetches once) and none
    of them may raise into the page render."""

    async def test_a_missing_key_reads_as_zero(self):
        client = Mock(get=AsyncMock(return_value=None))
        assert await aread_session_revision("t1", connections=FakeConnections(client)) == 0
        client.get.assert_awaited_once_with("daiv:ui-events:session-rev:t1")

    async def test_a_stored_revision_reads_as_an_int(self):
        client = Mock(get=AsyncMock(return_value=b"7"))
        assert await aread_session_revision("t1", connections=FakeConnections(client)) == 7

    async def test_an_unconfigured_bus_reads_zero(self):
        client = Mock(get=AsyncMock())
        assert await aread_session_revision("t1", connections=FakeConnections(client, configured=False)) == 0
        client.get.assert_not_awaited()

    async def test_a_redis_failure_reads_zero_without_a_traceback(self, caplog):
        client = Mock(get=AsyncMock(side_effect=redis.ConnectionError("redis is down")))
        with caplog.at_level(logging.WARNING, logger="daiv.core"):
            assert await aread_session_revision("t1", connections=FakeConnections(client)) == 0
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert "t1" in record.getMessage()


class TestStreamLifecycle:
    async def test_a_viewer_listens_on_the_broadcast_channel_and_their_own(self):
        pubsub = FakePubSub()
        connections = FakeConnections(Mock(pubsub=Mock(return_value=pubsub)))
        async with UIEventStream.for_user(7, connections=connections):
            pass
        assert pubsub.channels == (Channel.RUNS, "daiv:ui-events:user:7")

    async def test_a_session_page_also_listens_on_its_session(self):
        pubsub = FakePubSub()
        connections = FakeConnections(Mock(pubsub=Mock(return_value=pubsub)))
        async with UIEventStream.for_user(42, session="t1", connections=connections):
            pass
        assert pubsub.channels == (Channel.RUNS, "daiv:ui-events:user:42", "daiv:ui-events:session:t1")

    async def test_subscribes_and_always_releases_the_connection(self):
        pubsub = FakePubSub()
        async with stream_on(pubsub, "a", "b") as stream:
            assert stream is not None
        assert pubsub.channels == ("a", "b")
        assert pubsub.closed

    async def test_the_connection_is_released_even_when_the_reader_raises(self):
        """Leaving ``aclose`` to garbage collection would leak one Redis connection per
        dropped SSE stream."""
        pubsub = FakePubSub()
        with pytest.raises(RuntimeError):
            async with stream_on(pubsub):
                raise RuntimeError("reader died")
        assert pubsub.closed

    async def test_a_failed_subscribe_releases_the_connection_too(self):
        pubsub = FakePubSub()
        pubsub.subscribe = AsyncMock(side_effect=ConnectionError("redis went away"))
        with pytest.raises(ConnectionError):
            async with stream_on(pubsub):
                pass
        assert pubsub.closed

    async def test_reading_before_subscribing_is_a_programming_error(self):
        with pytest.raises(RuntimeError, match="not subscribed"):
            await stream_on(FakePubSub()).wait_for_change(0.1)


class TestWaitForChange:
    async def test_a_poke_is_a_change(self):
        async with stream_on(FakePubSub([poke()])) as stream:
            assert await stream.wait_for_change(20.0) == frozenset({UIEventKind.RUNS})

    async def test_an_idle_stream_reports_no_change(self):
        async with stream_on(FakePubSub()) as stream:
            assert await stream.wait_for_change(20.0) == frozenset()

    async def test_junk_is_not_a_change(self):
        async with stream_on(FakePubSub([{"type": "message", "channel": "c", "data": "garbage"}])) as stream:
            assert await stream.wait_for_change(20.0) == frozenset()

    async def test_either_kind_counts(self):
        """A poke of either kind is a change, so a reader that recomputes everything also
        repairs a badge whose own poke was dropped."""
        async with stream_on(FakePubSub([poke("notifications")])) as stream:
            assert await stream.wait_for_change(20.0) == frozenset({UIEventKind.NOTIFICATIONS})

    async def test_a_burst_is_absorbed_into_one_change(self):
        """A finishing run pokes several times over; coalescing here is what keeps that
        from costing one recount per poke, per connected reader."""
        pubsub = FakePubSub([poke(), poke(), poke()])
        async with stream_on(pubsub) as stream:
            assert await stream.wait_for_change(20.0) == frozenset({UIEventKind.RUNS})
            assert await stream.wait_for_change(20.0) == frozenset()
        assert pubsub.reads[:1] == [20.0]
        assert pubsub.reads[1:4] == [UIEventStream.COALESCE_S] * 3

    async def test_a_burst_reports_every_kind_it_carried(self):
        """A run end pokes the runs badge and the session page in one burst; the reader
        needs both kinds to know which of its views to refresh."""
        session_poke = poke("session", "daiv:ui-events:session:t1")
        async with stream_on(FakePubSub([poke(), session_poke])) as stream:
            assert await stream.wait_for_change(20.0) == frozenset({UIEventKind.RUNS, UIEventKind.SESSION})

    async def test_junk_inside_a_burst_neither_ends_it_nor_counts(self):
        junk = {"type": "message", "channel": "c", "data": "garbage"}
        async with stream_on(FakePubSub([poke(), junk, poke("notifications")])) as stream:
            assert await stream.wait_for_change(20.0) == frozenset({UIEventKind.RUNS, UIEventKind.NOTIFICATIONS})


class TestTransientClassification:
    """Which failures a reader may report quietly. Getting this wrong is invisible in
    tests and only shows up as Sentry noise during an outage (or a swallowed bug)."""

    @pytest.mark.parametrize(
        "exc",
        [
            redis.RedisError("generic"),
            redis.ConnectionError("refused"),
            redis.TimeoutError("timed out"),
            ConnectionResetError("peer reset"),
            TimeoutError("socket timeout"),
        ],
        ids=["redis-error", "redis-connection", "redis-timeout", "socket-reset", "socket-timeout"],
    )
    def test_an_outage_is_transient(self, exc):
        assert is_transient_bus_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [RuntimeError("wrong event loop"), AttributeError("typo"), ValueError("bad value")],
        ids=["loop-binding", "typo", "bad-value"],
    )
    def test_a_bug_is_not(self, exc):
        assert is_transient_bus_error(exc) is False

    def test_cancellation_is_never_classified(self):
        """``CancelledError`` is a ``BaseException``, so an ``except Exception`` never
        reaches here and outer cancellation keeps propagating."""
        assert is_transient_bus_error(asyncio.CancelledError()) is False
