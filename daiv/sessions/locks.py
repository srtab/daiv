from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from sessions.models import Session

logger = logging.getLogger("daiv.sessions")

# A claim that hasn't bumped last_active_at within this window is considered
# orphaned (worker crashed / OOM-killed before the holder's finally ran) and
# can be taken over by a fresh claim. Live holders heartbeat well within this
# window. This (and the takeover semantics below) was ported from the old
# chat.api.threads.ChatThreadService when the per-thread run slot was unified into
# SessionLock; it is now the single source of truth.
STALE_RUN_MINUTES = 30


def stale_cutoff(now: datetime | None = None) -> datetime:
    """Heartbeat threshold: a session whose ``last_active_at`` predates this is treated as
    orphaned (its holder crashed before releasing). Shared by ``SessionLock`` takeover,
    ``sync_stuck_runs``, and ``SessionDetailView``'s in-flight gate so the staleness window
    has a single definition. Pass ``now`` to reuse a caller's already-captured timestamp.
    """
    return (now or timezone.now()) - timedelta(minutes=STALE_RUN_MINUTES)


class SessionLock:
    """Unified execution slot for a session.

    Holders are chat turns (holder_id = the AG-UI run_id) and background jobs
    routed through ``run_job_task`` (holder_id = str(Run.pk)). Exactly one such
    holder executes against a thread's checkpoint at a time — this closes the
    historical race where a chat continuation and a ``run_job_task`` run on the
    same thread ran concurrently.

    Not covered: webhook addressors (issue/MR) call ``create_daiv_agent``
    directly and never route through this lock, so they are not mutually
    excluded with chat/job holders. Stale takeover (below) guards only the DB
    slot, not the in-flight work: a holder that stalls past ``STALE_RUN_MINUTES``
    can be superseded while its own graph invocation is still running (there is
    no fencing token). The generous window makes that rare, and a takeover logs
    a warning so it is observable.
    """

    @staticmethod
    async def try_claim(thread_id: str, holder_id: str) -> bool:
        """Claim the slot if it is free OR its heartbeat is stale.

        A stale takeover (claiming a slot a prior holder never released) is logged
        at WARNING so it surfaces in monitoring — the prior holder may still be
        executing against the checkpoint.
        """
        now = timezone.now()
        # Fast path: claim a free slot (the common, uncontended case) in one query.
        if await Session.objects.filter(thread_id=thread_id, active_run_id__isnull=True).aupdate(
            active_run_id=holder_id, last_active_at=now
        ):
            return True

        # Slow path: the slot is held. Take it over only if the holder's heartbeat
        # is stale. Read the prior holder first so the takeover is observable; the
        # CAS on ``active_run_id`` keeps the claim itself race-safe.
        cutoff = stale_cutoff(now)
        prior_holder = await (
            Session.objects
            .filter(thread_id=thread_id, active_run_id__isnull=False, last_active_at__lt=cutoff)
            .values_list("active_run_id", flat=True)
            .afirst()
        )
        if prior_holder is None:
            return False  # held by a live holder — nothing to take over

        taken = await Session.objects.filter(
            thread_id=thread_id, active_run_id=prior_holder, last_active_at__lt=cutoff
        ).aupdate(active_run_id=holder_id, last_active_at=now)
        if taken:
            logger.warning(
                "SessionLock: stale takeover of thread_id=%s from prior holder=%s by holder=%s; "
                "prior holder may still be executing against the same checkpoint",
                thread_id,
                prior_holder,
                holder_id,
            )
        return bool(taken)

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
