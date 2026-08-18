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
from unittest.mock import AsyncMock, Mock

import pytest
import redis

from core.ui_events import (
    Channel,
    RedisConnections,
    UIEventKind,
    UIEventPublisher,
    UIEventStream,
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


class TestRedisConnections:
    def test_no_setting_at_all_is_a_bus_that_is_simply_absent(self, settings):
        """The test settings leave the Redis component out of the include list, which is
        a valid deployment shape — not a broken one."""
        del settings.DJANGO_REDIS_URL
        assert RedisConnections().configured is False

    def test_a_reader_asking_for_a_client_gets_a_clear_error(self, settings):
        """Where publishers stay quiet, this is the side an operator sees when Redis is
        missing — it reaches the SSE handler's error frame."""
        del settings.DJANGO_REDIS_URL
        with pytest.raises(RuntimeError, match="requires Redis"):
            RedisConnections().async_client()

    def test_clients_are_built_once_and_kept(self, settings):
        settings.DJANGO_REDIS_URL = "redis://localhost:6379/0"
        connections = RedisConnections()
        assert connections.sync_client() is connections.sync_client()
        assert connections.async_client() is connections.async_client()
        assert connections.sync_client() is not connections.async_client()


class TestPublish:
    def test_runs_changed_goes_to_the_broadcast_channel(self):
        client = Mock()
        publisher_with(client).runs_changed()
        client.publish.assert_called_once_with(Channel.RUNS, UIEventKind.RUNS.as_payload())

    def test_notifications_changed_goes_to_that_user_alone(self):
        client = Mock()
        publisher_with(client).notifications_changed(42)
        client.publish.assert_called_once_with("daiv:ui-events:user:42", UIEventKind.NOTIFICATIONS.as_payload())

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


class TestAsyncPublish:
    async def test_runs_changed_goes_to_the_broadcast_channel(self):
        client = Mock(publish=AsyncMock())
        await publisher_with(client).aruns_changed()
        client.publish.assert_awaited_once_with(Channel.RUNS, UIEventKind.RUNS.as_payload())

    async def test_a_redis_failure_never_reaches_the_caller(self, caplog):
        client = Mock(publish=AsyncMock(side_effect=ConnectionError("redis is down")))
        await publisher_with(client).aruns_changed()
        assert "failed to publish" in caplog.text

    async def test_an_unconfigured_bus_drops_the_poke_without_a_warning(self, caplog):
        client = Mock(publish=AsyncMock())
        await publisher_with(client, configured=False).aruns_changed()
        client.publish.assert_not_awaited()
        assert "failed to publish" not in caplog.text


class TestStreamLifecycle:
    async def test_a_viewer_listens_on_the_broadcast_channel_and_their_own(self):
        pubsub = FakePubSub()
        connections = FakeConnections(Mock(pubsub=Mock(return_value=pubsub)))
        async with UIEventStream.for_user(7, connections=connections):
            pass
        assert pubsub.channels == (Channel.RUNS, "daiv:ui-events:user:7")

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

    async def test_a_reader_gets_a_clear_error_when_no_bus_is_configured(self, settings):
        del settings.DJANGO_REDIS_URL
        with pytest.raises(RuntimeError, match="requires Redis"):
            async with UIEventStream("a", connections=RedisConnections()):
                pass

    async def test_reading_before_subscribing_is_a_programming_error(self):
        with pytest.raises(RuntimeError, match="not subscribed"):
            await stream_on(FakePubSub()).wait_for_change(0.1)


class TestWaitForChange:
    async def test_a_poke_is_a_change(self):
        async with stream_on(FakePubSub([poke()])) as stream:
            assert await stream.wait_for_change(20.0) is True

    async def test_an_idle_stream_reports_no_change(self):
        async with stream_on(FakePubSub()) as stream:
            assert await stream.wait_for_change(20.0) is False

    async def test_junk_is_not_a_change(self):
        async with stream_on(FakePubSub([{"type": "message", "channel": "c", "data": "garbage"}])) as stream:
            assert await stream.wait_for_change(20.0) is False

    async def test_either_kind_counts(self):
        """One recompute covers every counter, so a poke of either kind also repairs a
        badge whose own poke was dropped."""
        async with stream_on(FakePubSub([poke("notifications")])) as stream:
            assert await stream.wait_for_change(20.0) is True

    async def test_a_burst_is_absorbed_into_one_change(self):
        """A finishing run pokes several times over; coalescing here is what keeps that
        from costing one recount per poke, per connected reader."""
        pubsub = FakePubSub([poke(), poke(), poke()])
        async with stream_on(pubsub) as stream:
            assert await stream.wait_for_change(20.0) is True
            assert await stream.wait_for_change(20.0) is False
        assert pubsub.reads[:1] == [20.0]
        assert pubsub.reads[1:4] == [UIEventStream.COALESCE_S] * 3


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
