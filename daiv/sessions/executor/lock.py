import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from operator import attrgetter
from typing import TYPE_CHECKING

from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.models import RunStatus, Session, SessionOrigin

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable

    from accounts.models import User
    from sessions.models import Run

logger = logging.getLogger("daiv.sessions")

STALE_RUN_MINUTES = 30
LOCK_WAIT_TIMEOUT_S = STALE_RUN_MINUTES * 60.0  # give a long-running chat turn time to finish
LOCK_POLL_INTERVAL_S = 5.0
LOCK_HEARTBEAT_INTERVAL_S = 60.0


def stale_cutoff(now: datetime | None = None) -> datetime:
    """Heartbeat threshold: a session whose ``last_active_at`` predates this is treated as
    orphaned (its holder crashed before releasing). The single definition of the staleness window.
    Pass ``now`` to reuse a caller's already-captured timestamp.
    """
    return (now or timezone.now()) - timedelta(minutes=STALE_RUN_MINUTES)


@dataclass(frozen=True)
class LiveRun:
    """The run a session page can stream: ``id`` is the slot holder's relay id, ``stoppable`` is whether the
    cancel endpoint can stop it (chat runs only, and with ``live_run_for`` only for the session's owner),
    ``started_at`` seeds the page's working timer."""

    id: str
    stoppable: bool
    started_at: datetime


@dataclass(frozen=True)
class SessionActivity:
    """What a session's runs are doing, read once per page view or poll so every part shares one staleness cutoff.

    In flight, either the slot has a holder (``holder`` is its started run when identifiable, ``live`` the run the
    page streams) or it is free and ``waiting`` lists the runs yet to start, oldest first. A chat run never waits: it
    claims the slot before its run is recorded.
    """

    in_flight: bool = False
    holder: Run | None = None
    live: LiveRun | None = None
    waiting: tuple[Run, ...] = ()

    @property
    def waiting_since(self) -> datetime | None:
        return self.waiting[0].created_at if self.waiting else None

    def behind_holder(self, run: Run) -> bool:
        """Whether ``run`` is unfinished while the slot is held by another run, so it has not executed yet."""
        return self.live is not None and run.status not in RunStatus.terminal() and run != self.holder


def session_activity(session: Session, runs: Iterable[Run]) -> SessionActivity:
    """``session``'s :class:`SessionActivity`. In flight means an unfinished run and a heartbeat fresher than
    :func:`stale_cutoff`: a live holder (chat stream, ``run_job_task``, webhook run) bumps ``last_active_at`` every
    ~60s, so a holder stranded past the window (crashed worker, orphaned queue entry) is dead, whatever its runs'
    status says.

    ``live`` falls back to the oldest RUNNING run, else the newest unfinished one, while the holder is not
    identifiable, so the page still streams the slot's relay id."""
    unfinished = sorted((run for run in runs if run.status not in RunStatus.terminal()), key=attrgetter("created_at"))
    if not unfinished or session.last_active_at < stale_cutoff():
        return SessionActivity()
    if not session.active_run_id:
        return SessionActivity(
            in_flight=True, waiting=tuple(run for run in unfinished if run.trigger_type != SessionOrigin.CHAT)
        )
    running = [run for run in unfinished if run.status == RunStatus.RUNNING]
    holder = _slot_holder(session.active_run_id, running)
    shown = holder or (running[0] if running else unfinished[-1])
    return SessionActivity(
        in_flight=True,
        holder=holder,
        live=LiveRun(
            id=session.active_run_id,
            stoppable=shown.trigger_type == SessionOrigin.CHAT,
            started_at=shown.started_at or shown.created_at,
        ),
    )


def _slot_holder(holder_id: str, running: list[Run]) -> Run | None:
    """The RUNNING run holding the slot under ``holder_id``. A task-backed run holds it under its pk but turns RUNNING
    before claiming it, so a run the id doesn't name holds it only as a chat or webhook run, which use ids of their
    own."""
    return (
        next((run for run in running if str(run.pk) == holder_id), None)
        or next((run for run in running if run.trigger_type == SessionOrigin.CHAT), None)
        or next((run for run in running if run.trigger_type in SessionOrigin.webhooks()), None)
    )


def live_run_for(live: LiveRun | None, session: Session, user: User) -> LiveRun | None:
    """``live`` as ``user`` sees it: stoppable only for the session's owner, as the cancel endpoint allows."""
    if live is not None and live.stoppable and not Session.objects.by_owner(user).filter(pk=session.pk).exists():
        return replace(live, stoppable=False)
    return live


def background_in_flight(activity: SessionActivity, runs: Iterable[Run]) -> bool:
    """Whether a live non-chat run (a job, a webhook, a CI fix, a delegation resume) keeps the session of ``activity``
    in flight.

    Only these wake a session page that rendered idle: a chat run streams to the tab that started it.
    """
    return activity.in_flight and any(
        run.trigger_type != SessionOrigin.CHAT and run.status not in RunStatus.terminal() for run in runs
    )


class SessionLock:
    """Unified execution slot for a session.

    Holders are chat turns (holder_id = the AG-UI run_id), background jobs
    routed through ``run_job_task`` (holder_id = str(Run.pk)) and webhook
    runs (holder_id = ``webhook-<uuid4 hex>``). Exactly one such holder
    executes against a thread's checkpoint at a time.

    Stale takeover (below) guards only the DB slot, not the in-flight work: a
    holder that stalls past ``STALE_RUN_MINUTES`` can be superseded while its
    own graph invocation is still running (there is no fencing token). The
    generous window makes that rare, and a takeover logs a warning so it is
    observable.
    """

    @staticmethod
    async def try_claim(thread_id: str, holder_id: str) -> bool:
        """Claim the slot if it is free OR its heartbeat is stale.

        A stale takeover (claiming a slot a prior holder never released) is logged
        at WARNING so it surfaces in monitoring — the prior holder may still be
        executing against the checkpoint.
        """
        return await sync_to_async(_claim)(thread_id, holder_id)

    @staticmethod
    def claim_for_run(thread_id: str, run_id: object) -> bool:
        """:meth:`try_claim` for the inbox drainer, which claims the slot for the run it is creating, under the run's
        pk, inside the transaction that creates it. SYNC ONLY."""
        return _claim(thread_id, str(run_id))

    @staticmethod
    async def aclaim_for_run(thread_id: str, run_id: object) -> bool:
        return await sync_to_async(_claim)(thread_id, str(run_id))

    @staticmethod
    async def heartbeat(thread_id: str, holder_id: str) -> bool:
        """Bump ``last_active_at`` while the slot is still ours.

        Returns ``False`` if we no longer hold the slot (e.g. a stale takeover
        reassigned it) — the caller can then stop writing to a checkpoint it no
        longer owns.
        """
        bumped = await Session.objects.filter(thread_id=thread_id, active_run_id=holder_id).aupdate(
            last_active_at=timezone.now()
        )
        return bool(bumped)

    @staticmethod
    async def release(thread_id: str, holder_id: str) -> None:
        """Clear the slot only if we still hold it (no-op if already reassigned)."""
        await Session.objects.filter(thread_id=thread_id, active_run_id=holder_id).aupdate(
            active_run_id=None, last_active_at=timezone.now()
        )


def _claim(thread_id: str, holder_id: str) -> bool:
    now = timezone.now()
    # Fast path: claim a free slot (the common, uncontended case) in one query.
    if Session.objects.filter(thread_id=thread_id, active_run_id__isnull=True).update(
        active_run_id=holder_id, last_active_at=now
    ):
        return True

    # Slow path: the slot is held. Take it over only if the holder's heartbeat
    # is stale. Read the prior holder first so the takeover is observable; the
    # CAS on ``active_run_id`` keeps the claim itself race-safe.
    cutoff = stale_cutoff(now)
    prior_holder = (
        Session.objects
        .filter(thread_id=thread_id, active_run_id__isnull=False, last_active_at__lt=cutoff)
        .values_list("active_run_id", flat=True)
        .first()
    )
    if prior_holder is None:
        return False  # held by a live holder — nothing to take over

    taken = Session.objects.filter(thread_id=thread_id, active_run_id=prior_holder, last_active_at__lt=cutoff).update(
        active_run_id=holder_id, last_active_at=now
    )
    if taken:
        logger.warning(
            "SessionLock: stale takeover of thread_id=%s from prior holder=%s by holder=%s; "
            "prior holder may still be executing against the same checkpoint",
            thread_id,
            prior_holder,
            holder_id,
        )
    return bool(taken)


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
class Claimed:
    """The inbox drainer claimed the slot for this run when it created it (``SessionLock.claim_for_run``): the executor
    checks the claim is still the run's, heartbeats it and releases it after the run. It never polls."""

    holder_id: str


@dataclass(frozen=True)
class NoLock:
    """Run without the slot, for a one-shot run (``thread_id=None``), which has no ``Session`` row."""


LockPolicy = Wait | Held | Claimed | NoLock


def wait_for_slot(holder_id: str) -> Wait:
    """``Wait`` for the session's slot under ``holder_id``, up to ``LOCK_WAIT_TIMEOUT_S``."""
    return Wait(holder_id=holder_id, timeout_s=LOCK_WAIT_TIMEOUT_S)


class SessionLockTimeoutError(TimeoutError):
    """``Wait`` gave up: the slot's holder kept it past ``timeout_s``."""


class SessionLockLostError(Exception):
    """A stale takeover reassigned the slot while a stream held it, or before a ``Claimed`` run started."""


@asynccontextmanager
async def hold_session_lock(
    policy: LockPolicy, thread_id: str, *, background_heartbeat: bool = True
) -> AsyncGenerator[str | None]:
    """Hold ``thread_id``'s execution slot for the body under ``policy`` and yield the holder id, ``None`` for
    ``NoLock``.

    With ``background_heartbeat`` a task heartbeats the slot until the body ends; ``stream_run`` turns it off and
    beats between its events. The heartbeat is cancelled and awaited before a ``Wait`` or ``Claimed`` claim is
    released, so no heartbeat outlives the hold; a ``Held`` slot is left for its caller to release. A failed release
    is logged, never raised over the body's own outcome.
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
        if isinstance(policy, Wait | Claimed):
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

    ``Wait`` raises ``SessionLockTimeoutError`` if the slot never frees within ``timeout_s``, and
    ``Session.DoesNotExist`` at once when there is no slot to free: the thread has no ``Session`` row. Takeover needs
    the holder's last heartbeat to be ``STALE_RUN_MINUTES`` old, so with ``timeout_s`` at that length
    (``LOCK_WAIT_TIMEOUT_S``) a waiter that started before a crashed holder's last heartbeat times out before
    it can take over.
    """
    if isinstance(policy, NoLock):
        return None
    if isinstance(policy, Held):
        return policy.holder_id
    if isinstance(policy, Claimed):
        if not await SessionLock.heartbeat(thread_id, policy.holder_id):
            raise SessionLockLostError(
                f"the slot claimed for run {policy.holder_id} on thread_id={thread_id} was taken"
            )
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
        if not polled:
            await Session.objects.only("thread_id").aget(pk=thread_id)
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
