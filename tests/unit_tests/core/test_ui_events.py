"""Wire-level tests for the nav event bus.

The bus carries pokes between processes (a worker finishing a run, the web worker
holding an SSE stream), so both halves of the contract are pinned here: what a
publisher puts on the wire, and what a reader makes of it — including the junk
cases, since a reader that raises drops a browser's stream.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core import ui_events


@pytest.fixture
def bus(settings):
    """Configure a bus. The test settings omit the Redis component, which is a valid
    deployment shape the module treats as "no bus" — pinned separately below."""
    settings.DJANGO_REDIS_URL = "redis://localhost:6379/0"
    return settings


@pytest.fixture
def publisher(bus):
    """Capture what publishers put on the wire."""
    client = Mock()
    with patch("core.ui_events.get_sync_redis", return_value=client):
        yield client


class TestPublish:
    def test_runs_changed_goes_to_the_broadcast_channel(self, publisher):
        ui_events.publish_runs_changed()
        publisher.publish.assert_called_once_with(ui_events.RUNS_CHANNEL, json.dumps({"kind": "runs"}))

    def test_notifications_changed_goes_to_that_user_alone(self, publisher):
        ui_events.publish_notifications_changed(42)
        publisher.publish.assert_called_once_with("daiv:ui-events:user:42", json.dumps({"kind": "notifications"}))

    def test_a_poke_carries_no_state(self, publisher):
        """Only a kind crosses the bus — readers recompute. A count in the payload
        would mean publishers computing other users' visibility."""
        ui_events.publish_runs_changed()
        payload = json.loads(publisher.publish.call_args.args[1])
        assert payload == {"kind": "runs"}

    def test_no_recipient_is_a_no_op(self, publisher):
        ui_events.publish_notifications_changed(None)
        publisher.publish.assert_not_called()

    def test_a_redis_failure_never_reaches_the_caller(self, publisher, caplog):
        """Publishers sit inside signal handlers: a Redis outage must cost a badge
        refresh, never the write that triggered it."""
        publisher.publish.side_effect = ConnectionError("redis is down")
        ui_events.publish_runs_changed()
        assert "failed to publish" in caplog.text

    def test_an_unconfigured_bus_drops_the_poke_without_a_warning(self, caplog):
        """Deployments (and the test settings) can omit Redis entirely. That is not an
        outage, so it must not log one per write — the badges simply stop being live, and
        the reader side is where the misconfiguration surfaces."""
        client = Mock()
        with patch("core.ui_events.get_sync_redis", return_value=client):
            ui_events.publish_runs_changed()
        client.publish.assert_not_called()
        assert "failed to publish" not in caplog.text


class TestAsyncPublish:
    async def test_runs_changed_goes_to_the_broadcast_channel(self, bus):
        client = Mock(publish=AsyncMock())
        with patch("core.ui_events.get_async_redis", return_value=client):
            await ui_events.apublish_runs_changed()
        client.publish.assert_awaited_once_with(ui_events.RUNS_CHANNEL, json.dumps({"kind": "runs"}))

    async def test_a_redis_failure_never_reaches_the_caller(self, bus, caplog):
        client = Mock(publish=AsyncMock(side_effect=ConnectionError("redis is down")))
        with patch("core.ui_events.get_async_redis", return_value=client):
            await ui_events.apublish_runs_changed()
        assert "failed to publish" in caplog.text


class TestParseKind:
    def test_reads_the_kind_off_a_poke(self):
        message = {"type": "message", "channel": ui_events.RUNS_CHANNEL, "data": json.dumps({"kind": "runs"})}
        assert ui_events.parse_kind(message) == "runs"

    @pytest.mark.parametrize(
        "message",
        [
            None,
            {"type": "subscribe", "channel": "c", "data": 1},
            {"type": "message", "channel": "c", "data": "not json"},
            {"type": "message", "channel": "c", "data": "[]"},
            {"type": "message", "channel": "c"},
        ],
        ids=["timeout", "subscribe-ack", "not-json", "wrong-shape", "no-data"],
    )
    def test_junk_yields_none_rather_than_raising(self, message):
        # A raise here would abort the SSE response mid-stream, which the browser can
        # only read as a dropped connection.
        assert ui_events.parse_kind(message) is None


class TestSubscription:
    async def test_a_reader_gets_a_clear_error_when_no_bus_is_configured(self):
        """Where publishers stay quiet, the reader must not: this is the side an operator
        sees when Redis is missing, and it reaches the SSE handler's error frame."""
        with patch.object(ui_events, "_async_client", None), pytest.raises(RuntimeError, match="requires Redis"):
            async with ui_events.subscription("a"):
                pass

    async def test_subscribes_and_always_releases_the_connection(self):
        pubsub = Mock(subscribe=AsyncMock(), aclose=AsyncMock())
        client = Mock(pubsub=Mock(return_value=pubsub))
        with patch("core.ui_events.get_async_redis", return_value=client):
            async with ui_events.subscription("a", "b") as opened:
                assert opened is pubsub
        pubsub.subscribe.assert_awaited_once_with("a", "b")
        pubsub.aclose.assert_awaited_once()

    async def test_the_connection_is_released_even_when_the_reader_raises(self):
        """Leaving ``aclose`` to garbage collection would leak one Redis connection per
        dropped SSE stream."""
        pubsub = Mock(subscribe=AsyncMock(), aclose=AsyncMock())
        client = Mock(pubsub=Mock(return_value=pubsub))
        with patch("core.ui_events.get_async_redis", return_value=client), pytest.raises(RuntimeError):
            async with ui_events.subscription("a"):
                raise RuntimeError("reader died")
        pubsub.aclose.assert_awaited_once()
