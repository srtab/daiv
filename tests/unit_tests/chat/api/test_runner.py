"""Tests for the chat run background runner (publisher loop + task registry)."""

import asyncio
import contextlib
import json
from types import SimpleNamespace

from ag_ui.core.events import CustomEvent, EventType

from chat.api import relay, runner


def _stub_streamer(events_gen):
    return SimpleNamespace(thread_id="t-1", run_id="r-1", events=events_gen)


async def test_run_to_relay_publishes_each_event_then_sentinel(fake_redis):
    async def _events():
        yield CustomEvent(type=EventType.CUSTOM, name="resolved_env", value={"id": "e-1"})
        yield CustomEvent(type=EventType.CUSTOM, name="second", value=2)

    await runner.run_to_relay(_stub_streamer(_events))

    entries = fake_redis.streams[relay.run_events_key("t-1", "r-1")]
    payloads = [fields for _id, fields in entries]
    assert json.loads(payloads[0]["data"])["name"] == "resolved_env"
    # camelCase alias serialization is the wire contract with the JS client
    assert '"type":"CUSTOM"' in payloads[0]["data"]
    assert json.loads(payloads[1]["data"])["value"] == 2
    assert payloads[2] == {"end": "1"}


async def test_run_to_relay_publishes_sentinel_even_when_stream_raises(fake_redis):
    async def _boom():
        raise RuntimeError("publisher-side failure")
        yield  # pragma: no cover

    # Must not raise: a background task's exception has no consumer.
    await runner.run_to_relay(_stub_streamer(_boom))

    entries = fake_redis.streams[relay.run_events_key("t-1", "r-1")]
    assert entries[-1][1] == {"end": "1"}


async def test_spawn_run_registers_and_prunes_task(fake_redis):
    async def _events():
        yield CustomEvent(type=EventType.CUSTOM, name="only", value=1)

    task = runner.spawn_run(_stub_streamer(_events))
    assert runner._TASKS.get("r-1") is task
    await task
    await asyncio.sleep(0)  # let the done-callback prune
    assert "r-1" not in runner._TASKS


async def test_cancel_local_cancels_running_task_and_reports_miss(fake_redis):
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    task = runner.spawn_run(_stub_streamer(_hang))
    await started.wait()

    assert runner.cancel_local("r-unknown") is False
    assert runner.cancel_local("r-1") is True
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Sentinel still published by the finally.
    assert fake_redis.streams[relay.run_events_key("t-1", "r-1")][-1][1] == {"end": "1"}
