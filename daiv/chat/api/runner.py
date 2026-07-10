"""Background execution of chat runs.

``spawn_run`` detaches a chat run from the HTTP request that started it: the
run executes as an ``asyncio.Task`` in the web process and publishes its AG-UI
events to the Redis relay, so client disconnects never cancel the run. The
task registry enables immediate local cancellation ("Stop" in the UI); the
Redis cancel flag (see ``chat.api.relay``) covers the cross-process case.

Process restarts still kill in-flight tasks — the streamer's ``finally`` runs
on cancellation (finalize FAILED + lock release), and the existing heartbeat /
stale-takeover / ``sync_stuck_runs`` machinery reconciles hard kills.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from . import relay

if TYPE_CHECKING:
    from .streaming import ChatRunStreamer

logger = logging.getLogger("daiv.chat")

# Strong references so tasks aren't garbage-collected mid-run; pruned on completion.
_TASKS: dict[str, asyncio.Task] = {}


def spawn_run(streamer: ChatRunStreamer) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(run_to_relay(streamer), name=f"chat-run-{streamer.run_id}")
    _TASKS[streamer.run_id] = task
    task.add_done_callback(lambda _t, run_id=streamer.run_id: _TASKS.pop(run_id, None))
    return task


def cancel_local(run_id: str) -> bool:
    """Hard-cancel the run if it executes in this process. Returns False when it
    doesn't — the caller must have set the Redis cancel flag for that case."""
    task = _TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


async def run_to_relay(streamer: ChatRunStreamer) -> None:
    """Drive the run and publish each event to the relay.

    Never raises ``Exception`` (nothing consumes a background task's result;
    ``events()`` already reports agent failures as RUN_ERROR events — anything
    reaching here is relay/redis trouble). ``CancelledError`` propagates so
    task cancellation keeps its semantics. The end sentinel is published in
    ``finally`` no matter what, so readers always see a terminal marker.
    """
    thread_id, run_id = streamer.thread_id, streamer.run_id
    try:
        async for event in streamer.events():
            await relay.publish_event(thread_id, run_id, event.model_dump_json(by_alias=True, exclude_none=True))
    except Exception:
        logger.exception("chat: run publisher failed for thread_id=%s run_id=%s", thread_id, run_id)
    finally:
        try:
            await relay.publish_end(thread_id, run_id)
        except Exception:
            logger.exception("chat: failed to publish end sentinel for thread_id=%s run_id=%s", thread_id, run_id)
