import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessions.locks import STALE_RUN_MINUTES, SessionLock

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("daiv.sessions")

LOCK_WAIT_TIMEOUT_S = STALE_RUN_MINUTES * 60.0  # give a long-running chat turn time to finish
LOCK_POLL_INTERVAL_S = 5.0
LOCK_HEARTBEAT_INTERVAL_S = 60.0


@dataclass(frozen=True)
class Wait:
    """Claim the slot, polling until the current holder frees it; the executor releases it after the run."""

    holder_id: str
    timeout_s: float


@dataclass(frozen=True)
class Held:
    """The caller already claimed the slot (chat's view does, so it can answer 409) and releases it: the executor
    only heartbeats it. The release stays with the caller so it can record the turn's end first."""

    holder_id: str


@dataclass(frozen=True)
class NoLock:
    """Run without the slot, for a run that has no ``Session`` row."""


LockPolicy = Wait | Held | NoLock


class SessionLockTimeoutError(TimeoutError):
    """``Wait`` gave up: the slot's holder kept it past ``timeout_s``."""


class SessionLockLostError(Exception):
    """A stale takeover reassigned the slot while a stream held it."""


@asynccontextmanager
async def hold_session_lock(
    policy: LockPolicy, thread_id: str, *, background_heartbeat: bool = True
) -> AsyncIterator[str | None]:
    """Hold ``thread_id``'s execution slot for the body under ``policy`` and yield the holder id, ``None`` for
    ``NoLock``.

    With ``background_heartbeat`` a task heartbeats the slot until the body ends; ``stream_run`` turns it off and
    beats between its events. The heartbeat is cancelled and awaited before a ``Wait`` claim is released, so no
    heartbeat outlives the hold; a ``Held`` slot is left for its caller to release. A failed release is logged,
    never raised over the body's own outcome.
    """
    holder_id = await _acquire_session_lock(policy, thread_id)
    if holder_id is None:
        yield None
        return
    heartbeat = asyncio.create_task(_heartbeat_loop(thread_id, holder_id)) if background_heartbeat else None
    try:
        yield holder_id
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if isinstance(policy, Wait):
            try:
                await SessionLock.release(thread_id, holder_id)
            except Exception:
                logger.exception("executor: failed to release session lock for thread_id=%s", thread_id)


async def still_held(thread_id: str, holder_id: str) -> bool:
    """Heartbeat the slot and say whether ``holder_id`` still holds it.

    A heartbeat that fails (a DB hiccup) is logged and counts as held: only a definitive "not ours" may stop a live
    run.
    """
    try:
        return await SessionLock.heartbeat(thread_id, holder_id)
    except Exception:
        logger.exception("executor: heartbeat failed for thread_id=%s", thread_id)
        return True


async def _acquire_session_lock(policy: LockPolicy, thread_id: str) -> str | None:
    """Return the holder id that now holds the slot, or ``None`` to run unlocked.

    ``Wait`` raises ``SessionLockTimeoutError`` if the slot never frees within ``timeout_s``. Takeover needs
    the holder's last heartbeat to be ``STALE_RUN_MINUTES`` old, so with ``timeout_s`` at that length
    (``LOCK_WAIT_TIMEOUT_S``) a waiter that started before a crashed holder's last heartbeat times out before
    it can take over.
    """
    if isinstance(policy, NoLock):
        return None
    if isinstance(policy, Held):
        return policy.holder_id
    start = time.monotonic()
    deadline = start + policy.timeout_s
    polled = False
    while time.monotonic() < deadline:
        if await SessionLock.try_claim(thread_id, policy.holder_id):
            if polled:
                logger.info(
                    "executor: waited %.0fs for session lock thread_id=%s holder=%s",
                    time.monotonic() - start,
                    thread_id,
                    policy.holder_id,
                )
            return policy.holder_id
        polled = True
        await asyncio.sleep(LOCK_POLL_INTERVAL_S)
    raise SessionLockTimeoutError(f"session lock for thread_id={thread_id} not released within {policy.timeout_s}s")


async def _heartbeat_loop(thread_id: str, holder_id: str) -> None:
    while True:
        await asyncio.sleep(LOCK_HEARTBEAT_INTERVAL_S)
        if not await still_held(thread_id, holder_id):
            # A stale takeover reassigned the slot. The in-flight invocation can't be safely aborted.
            logger.warning(
                "executor: lost session lock for thread_id=%s (holder=%s superseded); "
                "another holder may be running against the same checkpoint",
                thread_id,
                holder_id,
            )
