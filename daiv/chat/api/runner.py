"""Background execution of chat runs.

``RunSupervisor.spawn`` detaches a chat run from the HTTP request that started
it: the run executes as an ``asyncio.Task`` in the web process and publishes its
AG-UI events to the Redis relay, so client disconnects never cancel the run. The
supervisor's task registry enables immediate local cancellation ("Stop" in the
UI); the Redis cancel flag (see ``chat.api.relay``) covers the cross-process
case.

Organization: the stateless execution recipe (drive the streamer, publish to the
relay) is the module-level ``run_to_relay`` coroutine; the stateful, process-wide
task registry (spawn / dedup / prune / local-cancel) is ``RunSupervisor``, of
which there is one shared ``supervisor`` instance per web process.

Process restarts still kill in-flight tasks — the streamer's ``finally`` runs
on cancellation (finalize FAILED + lock release), and the existing heartbeat /
stale-takeover / ``sync_stuck_runs`` machinery reconciles hard kills.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING

from asgiref.sync import ThreadSensitiveContext

from . import relay

if TYPE_CHECKING:
    from .streaming import ChatRunStreamer

logger = logging.getLogger("daiv.chat")


async def run_to_relay(streamer: ChatRunStreamer) -> None:
    """Drive the run and publish each event to the relay.

    Never raises ``Exception`` (nothing consumes a background task's result;
    ``events()`` already reports agent failures as RUN_ERROR events — anything
    reaching here is relay/redis trouble). ``CancelledError`` propagates so
    task cancellation keeps its semantics. The end sentinel is published on a
    best-effort basis in ``finally``; if even that fails (Redis down) the reader
    falls back to the liveness probe in ``_run_event_frames``.

    The whole body runs inside its own ``ThreadSensitiveContext`` so every Django
    ORM ``a*`` call the run makes (``events()`` alone hits ``set_runtime_ctx``,
    ``start_chat_run``, the heartbeat, ``finalize_chat_run`` and the lock release)
    resolves ``sync_to_async(thread_sensitive=True)`` to a run-owned single-thread
    executor that lives exactly as long as the run — instead of the process-global
    one shared across every detached run. Paired with the fresh spawn context (see
    ``RunSupervisor.spawn``), this is what keeps the run off the request's executor:
    without the fresh context the run would inherit the request's
    ``ThreadSensitiveContext`` and this ``async with`` would be a re-entrant no-op.
    """
    thread_id, run_id = streamer.thread_id, streamer.run_id
    run_relay = relay.RunRelay(thread_id, run_id)
    try:
        async with ThreadSensitiveContext():
            async for event in streamer.events():
                await run_relay.publish_event(event.model_dump_json(by_alias=True, exclude_none=True))
    except Exception:
        logger.exception("chat: run publisher failed for thread_id=%s run_id=%s", thread_id, run_id)
    finally:
        try:
            await run_relay.publish_end()
        except Exception:
            logger.exception("chat: failed to publish end sentinel for thread_id=%s run_id=%s", thread_id, run_id)


class RunSupervisor:
    """Registry of in-flight chat run tasks for this web process.

    Holds a strong reference to each live task (so it isn't garbage-collected
    mid-run) keyed by ``run_id``, and enables immediate in-process cancellation.
    Use the shared ``supervisor`` singleton; the class is instantiable mainly to
    keep the registry state explicit and testable.
    """

    def __init__(self) -> None:
        # Strong references so tasks aren't garbage-collected mid-run; pruned on completion.
        self._tasks: dict[str, asyncio.Task] = {}

    def spawn(self, streamer: ChatRunStreamer) -> asyncio.Task:
        """Create, register, and return the detached run task.

        Must be called from the web-worker event loop: the task is bound to the
        running loop via ``asyncio.get_running_loop()`` and ``cancel_local``
        later cancels it on that same loop. Driving this singleton from a second
        loop would interleave independent registry state on one shared object
        (and cross-loop ``task.cancel()`` is unsafe) — same single-loop
        assumption ``get_redis`` documents for the relay client.
        """
        # One live task per run_id: uniqueness is guaranteed upstream by the atomic
        # ``SessionLock.try_claim`` (run_id == holder id), but self-guard here so a
        # regression can't silently overwrite the registry entry — which would orphan
        # the first task's strong ref and let the wrong done-callback prune the slot.
        existing = self._tasks.get(streamer.run_id)
        if existing is not None and not existing.done():
            raise RuntimeError(f"chat: a run task is already live for run_id={streamer.run_id}")
        # Run in a fresh context, NOT the request's. ``create_task`` copies the
        # current context by default, and a sync-only middleware (WhiteNoise) makes
        # Django reach this async view through an ``async_to_sync`` bridge that binds
        # a request-scoped ``CurrentThreadExecutor`` into ``AsyncToSync.executors``.
        # Inheriting it means every ORM ``a*`` call the detached run makes after the
        # request unwinds hits that now-torn-down executor and raises
        # "CurrentThreadExecutor already quit or is broken". A clean context sheds the
        # binding so ``run_to_relay``'s own ``ThreadSensitiveContext`` governs the
        # run's thread-sensitive work. Nothing the run needs is inherited via
        # contextvars — ``events()`` establishes its own runtime/usage/tracing context.
        task = asyncio.get_running_loop().create_task(
            run_to_relay(streamer), name=f"chat-run-{streamer.run_id}", context=contextvars.Context()
        )
        self._tasks[streamer.run_id] = task
        task.add_done_callback(lambda _t, run_id=streamer.run_id: self._tasks.pop(run_id, None))
        return task

    def cancel_local(self, run_id: str) -> bool:
        """Hard-cancel the run if it executes in this process. Returns False when it
        doesn't — the caller must have set the Redis cancel flag for that case."""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            return True
        return False


# One registry per web process; the web workers run a single event loop.
supervisor = RunSupervisor()
