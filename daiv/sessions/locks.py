from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from sessions.models import Session

# A claim that hasn't bumped last_active_at within this window is considered
# orphaned (worker crashed / OOM-killed before the holder's finally ran) and
# can be taken over by a fresh claim. Live holders heartbeat well within this
# window. Port of chat.api.threads.STALE_RUN_MINUTES semantics.
STALE_RUN_MINUTES = 30


class SessionLock:
    """Unified execution slot for a session.

    Holders are chat turns (holder_id = the AG-UI run_id) and background jobs
    (holder_id = str(Run.pk)). Exactly one holder executes against a thread's
    checkpoint at a time — this closes the historical race where a chat
    continuation and a webhook/API run on the same thread ran concurrently.
    """

    @staticmethod
    async def try_claim(thread_id: str, holder_id: str) -> bool:
        """Atomic claim: succeeds if the slot is free OR its heartbeat is stale."""
        stale_cutoff = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES)
        free_or_stale = Q(active_run_id__isnull=True) | Q(last_active_at__lt=stale_cutoff)
        claimed = await Session.objects.filter(Q(thread_id=thread_id) & free_or_stale).aupdate(
            active_run_id=holder_id, last_active_at=timezone.now()
        )
        return bool(claimed)

    @staticmethod
    async def heartbeat(thread_id: str, holder_id: str) -> None:
        """Bump ``last_active_at`` while the slot is still ours."""
        await Session.objects.filter(thread_id=thread_id, active_run_id=holder_id).aupdate(
            last_active_at=timezone.now()
        )

    @staticmethod
    async def release(thread_id: str, holder_id: str) -> None:
        """Clear the slot only if we still hold it."""
        await Session.objects.filter(thread_id=thread_id, active_run_id=holder_id).aupdate(
            active_run_id=None, last_active_at=timezone.now()
        )
