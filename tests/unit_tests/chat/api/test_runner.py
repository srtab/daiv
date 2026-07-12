"""Tests for the chat run background runner (publisher loop + task registry)."""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from ag_ui.core.events import CustomEvent, EventType

from chat.api import relay, runner


def _stub_streamer(events_gen):
    return SimpleNamespace(thread_id="t-1", run_id="r-1", events=events_gen)


async def test_run_to_relay_publishes_each_event_then_sentinel(fake_redis):
    async def _events():
        yield CustomEvent(type=EventType.CUSTOM, name="resolved_env", value={"id": "e-1"})
        yield CustomEvent(type=EventType.CUSTOM, name="second", value=2)

    await runner.run_to_relay(_stub_streamer(_events))

    entries = fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key]
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

    entries = fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key]
    assert entries[-1][1] == {"end": "1"}


async def test_spawn_run_registers_and_prunes_task(fake_redis):
    async def _events():
        yield CustomEvent(type=EventType.CUSTOM, name="only", value=1)

    task = runner.supervisor.spawn(_stub_streamer(_events))
    assert runner.supervisor._tasks.get("r-1") is task
    await task
    await asyncio.sleep(0)  # let the done-callback prune
    assert "r-1" not in runner.supervisor._tasks


async def test_cancel_local_cancels_running_task_and_reports_miss(fake_redis):
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    task = runner.supervisor.spawn(_stub_streamer(_hang))
    await started.wait()

    assert runner.supervisor.cancel_local("r-unknown") is False
    assert runner.supervisor.cancel_local("r-1") is True
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Sentinel still published by the finally.
    assert fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key][-1][1] == {"end": "1"}


async def test_run_to_relay_swallows_publish_end_failure(fake_redis):
    """If even the terminal sentinel can't be published (Redis down in the finally),
    ``run_to_relay`` still must not raise — a background task's exception has no
    consumer, and the reader falls back to the liveness probe.
    """

    async def _events():
        yield CustomEvent(type=EventType.CUSTOM, name="only", value=1)

    with patch("chat.api.relay.RunRelay.publish_end", new=AsyncMock(side_effect=RuntimeError("redis down"))):
        # Must not raise despite the sentinel publish failing.
        await runner.run_to_relay(_stub_streamer(_events))

    # The data event still landed; only the sentinel is missing.
    entries = fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key]
    assert json.loads(entries[-1][1]["data"])["value"] == 1


async def test_spawned_run_ignores_broken_request_thread_executor(fake_redis):
    """Regression: a detached run must not inherit the spawning request's asgiref
    thread-sensitive executor.

    In production a sync-only middleware (WhiteNoise) makes Django route the async
    view through an ``async_to_sync`` bridge, which leaks a request-scoped
    ``CurrentThreadExecutor`` into the view context via ``AsyncToSync.executors``.
    ``create_task`` copies that context, so the detached run inherited the binding;
    once the request unwound, asgiref marked that executor broken, and the run's next
    ORM ``a*`` call raised ``CurrentThreadExecutor already quit or is broken``. The
    run must be spawned in a fresh context so its thread-sensitive work resolves to a
    live, run-owned executor instead.
    """
    from asgiref.current_thread_executor import CurrentThreadExecutor
    from asgiref.sync import AsyncToSync, sync_to_async

    # Stand in for the request's leaked-then-torn-down executor.
    broken = CurrentThreadExecutor(None)
    broken._broken = True
    AsyncToSync.executors.current = broken
    try:
        results: list[str] = []

        async def _events():
            # The same path every Django ORM a* method takes; this raised on the
            # inherited broken executor before the fix.
            results.append(await sync_to_async(lambda: "db-ok", thread_sensitive=True)())
            yield CustomEvent(type=EventType.CUSTOM, name="ok", value=results[-1])

        await runner.supervisor.spawn(_stub_streamer(_events))
        # The ORM-style call completed instead of being swallowed as a failure.
        assert results == ["db-ok"]
        entries = fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key]
        assert json.loads(entries[0][1]["data"])["value"] == "db-ok"
    finally:
        del AsyncToSync.executors.current


async def test_run_to_relay_establishes_own_thread_sensitive_context(fake_redis):
    """The run body executes inside its own ``ThreadSensitiveContext`` so its
    thread-sensitive ORM gets a run-owned executor (cleanly torn down at run end)
    rather than sharing the process-global single-thread executor with every other
    detached run."""
    from asgiref.sync import SyncToAsync

    seen: list[object] = []

    async def _events():
        seen.append(SyncToAsync.thread_sensitive_context.get(None))
        yield CustomEvent(type=EventType.CUSTOM, name="ok", value=1)

    await runner.run_to_relay(_stub_streamer(_events))
    assert seen and seen[0] is not None


async def test_run_to_relay_publishes_run_cancelled_on_user_stop(fake_redis):
    """A user Stop cancels the run task. The cancelled streamer can't yield its own
    RUN_ERROR(run_cancelled), so ``run_to_relay`` synthesizes it (gated on the cancel
    flag) before the end sentinel — otherwise the live client renders the stopped turn
    as a clean finish."""
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    run_relay = relay.RunRelay("t-1", "r-1")
    await run_relay.request_cancel()  # the cancel endpoint set the flag

    task = asyncio.get_running_loop().create_task(runner.run_to_relay(_stub_streamer(_hang)))
    await started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    payloads = [fields for _id, fields in fake_redis.streams[run_relay.events_key]]
    assert json.loads(payloads[0]["data"])["code"] == "run_cancelled"
    assert payloads[-1] == {"end": "1"}


async def test_run_to_relay_omits_run_cancelled_on_shutdown(fake_redis):
    """A process shutdown also cancels the task, but without a user Stop the cancel flag
    is unset — so no ``run_cancelled`` is synthesized. Only the neutral sentinel is
    published, keeping shutdown from masquerading as a user Stop."""
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    task = asyncio.get_running_loop().create_task(runner.run_to_relay(_stub_streamer(_hang)))
    await started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    payloads = [fields for _id, fields in fake_redis.streams[relay.RunRelay("t-1", "r-1").events_key]]
    assert payloads == [{"end": "1"}]


async def test_spawn_run_rejects_duplicate_live_run_id(fake_redis):
    """One live task per run_id: a second spawn for a still-running run_id must
    raise rather than silently overwrite the registry entry (which would orphan
    the first task's strong ref and mis-prune the slot)."""
    started = asyncio.Event()

    async def _hang():
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover

    task = runner.supervisor.spawn(_stub_streamer(_hang))
    await started.wait()

    with pytest.raises(RuntimeError, match="already live for run_id=r-1"):
        runner.supervisor.spawn(_stub_streamer(_hang))

    # A fresh spawn is allowed once the prior task has completed/pruned.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # let the done-callback prune
    assert "r-1" not in runner.supervisor._tasks
