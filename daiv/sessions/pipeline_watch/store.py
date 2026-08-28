"""The only reads and writes of ``Session.watch_*``. No platform I/O, no policy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import F
from django.utils import timezone

from sessions.models import Run, Session, SessionOrigin, WatchState

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet


class WatchStore:
    """Persistence for a watch. Carries no state — an instance exists so a caller can replace it."""

    async def atransition(self, thread_id: str, *, expect, where: dict | None = None, **updates) -> bool:
        """Move a watch out of the states it is allowed to leave, reporting whether this caller won.

        Every write to ``watch_state`` goes through here. Two events finishing together both pass
        their checks off their own stale read, and only the row count says which one owns the
        transition — without it each posts its own MR comment, which no constraint dedupes.
        ``where`` adds the conditions a caller needs re-asserted inside the same statement.
        """
        expected = [expect] if isinstance(expect, str) else list(expect)
        updated = await Session.objects.filter(thread_id=thread_id, watch_state__in=expected, **(where or {})).aupdate(
            **updates
        )
        return updated == 1

    async def aopen_watch(self, repo_id: str, ref: str) -> Session | None:
        return await (
            Session.objects
            .filter(repo_id=repo_id, ref=ref, watch_state=WatchState.WATCHING)
            .select_related("user")
            .order_by("-watch_armed_at")
            .afirst()
        )

    async def ahas_open_watch(self, repo_id: str, ref: str) -> bool:
        return await Session.objects.filter(repo_id=repo_id, ref=ref, watch_state=WatchState.WATCHING).aexists()

    async def aarm(
        self,
        *,
        thread_id: str,
        repo_id: str,
        ref: str,
        merge_request_iid: int,
        user_id: int | None,
        reset_attempts: bool,
    ) -> None:
        session, _created = await Session.objects.aget_or_create(
            thread_id=thread_id,
            defaults={
                "origin": SessionOrigin.PIPELINE_WEBHOOK,
                "repo_id": repo_id,
                "ref": ref,
                "merge_request_iid": merge_request_iid,
                "user_id": user_id,
            },
        )
        updates = {
            "watch_state": WatchState.WATCHING,
            "watch_armed_at": timezone.now(),
            "ref": ref,
            "merge_request_iid": merge_request_iid,
        }
        if user_id and session.user_id is None:
            updates["user_id"] = user_id
        if reset_attempts:
            updates["watch_attempts"] = 0
            updates["watch_pipeline_id"] = None
        await Session.objects.filter(thread_id=thread_id).aupdate(**updates)

    async def arefund_attempt(self, thread_id: str) -> None:
        """Undo a claim whose fix run never started, so the attempt is not spent on nothing."""
        await Session.objects.filter(thread_id=thread_id, watch_state=WatchState.FIXING).aupdate(
            watch_state=WatchState.WATCHING, watch_attempts=F("watch_attempts") - 1, watch_pipeline_id=None
        )

    async def ais_fix_run(self, run_id: str | None) -> bool:
        """Whether this run was dispatched by the watch, which is what stops the arm resetting
        ``watch_attempts``. A run with no row reads as False, so every fix-run dispatcher must pass
        its ``run_id`` through to here or the loop bound is lost.
        """
        if not run_id:
            return False
        trigger_type = await Run.objects.filter(pk=run_id).values_list("trigger_type", flat=True).afirst()
        return trigger_type == SessionOrigin.PIPELINE_WEBHOOK

    def expiring_watches(self, cutoff: datetime, limit: int) -> QuerySet[Session]:
        return Session.objects.filter(watch_state__in=WatchState.open(), watch_armed_at__lt=cutoff).only(
            "thread_id", "repo_id", "merge_request_iid", "watch_state"
        )[:limit]

    async def arecover_stale_fixing(self, *, cutoff: datetime, now: datetime) -> int:
        return await Session.objects.filter(watch_state=WatchState.FIXING, watch_armed_at__lt=cutoff).aupdate(
            watch_state=WatchState.WATCHING, watch_armed_at=now
        )

    def stale_watching(self, cutoff: datetime, limit: int) -> QuerySet:
        return Session.objects.filter(watch_state=WatchState.WATCHING, watch_armed_at__lt=cutoff).values_list(
            "repo_id", "ref", "thread_id"
        )[:limit]
