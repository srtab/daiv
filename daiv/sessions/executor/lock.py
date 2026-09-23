import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessions.locks import SessionLock
from sessions.models import Session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("daiv.sessions")

LOCK_WAIT_TIMEOUT_S = 1800.0  # give a long-running chat turn time to finish
LOCK_POLL_INTERVAL_S = 5.0
LOCK_HEARTBEAT_INTERVAL_S = 60.0


@dataclass(frozen=True)
class Wait:
    """Claim the slot, polling until the current holder frees it."""

    holder_id: str
    timeout_s: float


@dataclass(frozen=True)
class Held:
    """The caller already claimed the slot (chat's view does, so it can answer 409): only heartbeat and
    release it."""

    holder_id: str


@dataclass(frozen=True)
class NoLock:
    """Run without the slot, for a run that has no ``Session`` row."""


LockPolicy = Wait | Held | NoLock


@asynccontextmanager
async def hold_session_lock(policy: LockPolicy, thread_id: str) -> AsyncIterator[None]:
    """Hold ``thread_id``'s execution slot for the body under ``policy``, heartbeating it, then release it.

    The heartbeat is cancelled and awaited before the release, so an in-flight bump can't land on a
    freed slot. A failed release is logged, never raised over the body's own outcome.
    """
    holder_id = await _acquire_session_lock(policy, thread_id)
    if holder_id is None:
        yield
        return
    heartbeat = asyncio.create_task(_heartbeat_loop(thread_id, holder_id))
    try:
        yield
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            await SessionLock.release(thread_id, holder_id)
        except Exception:
            logger.exception("executor: failed to release session lock for thread_id=%s", thread_id)


async def _acquire_session_lock(policy: LockPolicy, thread_id: str) -> str | None:
    """Return the holder id that now holds the slot, or ``None`` to run unlocked.

    ``Wait`` on a thread with no ``Session`` row runs unlocked rather than failing (legacy rows), and
    raises ``TimeoutError`` if the slot never frees within ``timeout_s``. ``LOCK_WAIT_TIMEOUT_S`` and
    ``STALE_RUN_MINUTES`` are the same length (30 min): a waiter that started polling strictly after the
    holder went stale takes it over well before timing out, but one that began at about the moment the
    holder crashed can time out just as takeover would first succeed.
    """
    if isinstance(policy, NoLock):
        return None
    if isinstance(policy, Held):
        return policy.holder_id
    if not await Session.objects.filter(pk=thread_id).aexists():
        logger.warning("executor: no session row for thread_id=%s; running without lock", thread_id)
        return None
    deadline = time.monotonic() + policy.timeout_s
    while time.monotonic() < deadline:
        if await SessionLock.try_claim(thread_id, policy.holder_id):
            return policy.holder_id
        await asyncio.sleep(LOCK_POLL_INTERVAL_S)
    raise TimeoutError(f"session lock for thread_id={thread_id} not released within {policy.timeout_s}s")


async def _heartbeat_loop(thread_id: str, holder_id: str) -> None:
    while True:
        await asyncio.sleep(LOCK_HEARTBEAT_INTERVAL_S)
        try:
            if not await SessionLock.heartbeat(thread_id, holder_id):
                # A stale takeover reassigned the slot. The in-flight invocation can't be safely aborted.
                logger.warning(
                    "executor: lost session lock for thread_id=%s (holder=%s superseded); "
                    "another holder may be running against the same checkpoint",
                    thread_id,
                    holder_id,
                )
        except Exception:
            logger.exception("executor: heartbeat failed for thread_id=%s", thread_id)
