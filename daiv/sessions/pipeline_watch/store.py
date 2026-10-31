"""The only module that queries the watch columns of ``MergeRequestOwnership``. No platform I/O, no policy.

``Session.watch_*`` are no longer read or written; they are dropped one release after this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Exists, OuterRef, Q
from django.db.models.fields.json import KT

from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, QuietReason

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

CI_KINDS = (EventKind.CI_FAILED, EventKind.CI_PASSED, EventKind.CI_STOPPED)


def gave_up(session: OuterRef, *, since: datetime) -> Exists:
    """Whether CI on a merge request of ``session`` ran out of attempts since ``since`` and has not passed after."""
    passed_after = InboxEvent.objects.filter(
        session=OuterRef("session"),
        merge_request_iid=OuterRef("merge_request_iid"),
        kind=EventKind.CI_PASSED,
        created_at__gt=OuterRef("created_at"),
    )
    return Exists(
        InboxEvent.objects.filter(
            session=session, kind=EventKind.CI_FAILED, reason=QuietReason.BUDGET_SPENT, created_at__gte=since
        ).exclude(Exists(passed_after))
    )


class WatchStore:
    """Persistence for the watch. Carries no state — an instance exists so a caller can replace it."""

    async def aopen(self, repo_id: str, ref: str) -> MergeRequestOwnership | None:
        """The open ownership whose merge request's source branch is ``ref``."""
        return await (
            MergeRequestOwnership.objects
            .filter(repo_id=repo_id, source_branch=ref, closed_at__isnull=True)
            .order_by("-updated_at")
            .afirst()
        )

    async def ahas_open(self, repo_id: str, ref: str) -> bool:
        return await MergeRequestOwnership.objects.filter(
            repo_id=repo_id, source_branch=ref, closed_at__isnull=True
        ).aexists()

    async def aget(self, repo_id: str, merge_request_iid: int) -> MergeRequestOwnership | None:
        return await MergeRequestOwnership.objects.filter(repo_id=repo_id, merge_request_iid=merge_request_iid).afirst()

    async def arecord_pipeline(self, ownership_id: int, pipeline_id: int) -> bool:
        """Record ``pipeline_id`` as judged, reporting whether this caller won: two deliveries of one pipeline must not
        both post its event."""
        updated = await (
            MergeRequestOwnership.objects
            .filter(pk=ownership_id)
            .filter(Q(last_pipeline_id__isnull=True) | ~Q(last_pipeline_id=pipeline_id))
            .aupdate(last_pipeline_id=pipeline_id)
        )
        return updated == 1

    async def aset_enabled(self, ownership_id: int, *, enabled: bool) -> None:
        """Turn the watch on or off; turning it on gives it its attempts back, as a person asking again is a new
        budget."""
        updates = {"watch_enabled": enabled} | ({"attempts": 0} if enabled else {})
        await MergeRequestOwnership.objects.filter(pk=ownership_id).aupdate(**updates)

    def unjudged(self, *, quiet_since: datetime, horizon: datetime, limit: int) -> QuerySet[MergeRequestOwnership]:
        """Open, watched merge requests whose latest DAIV commit has no CI result yet, quiet since ``quiet_since`` and
        claimed after ``horizon``: the ones a missed webhook may have left waiting."""
        judged = InboxEvent.objects.annotate(sha=KT("payload__sha")).filter(
            session=OuterRef("session"),
            kind__in=CI_KINDS,
            merge_request_iid=OuterRef("merge_request_iid"),
            sha=OuterRef("last_daiv_sha"),
        )
        return (
            MergeRequestOwnership.objects
            .filter(closed_at__isnull=True, watch_enabled=True, updated_at__lt=quiet_since, updated_at__gte=horizon)
            .exclude(Exists(judged))
            .only("id", "repo_id", "merge_request_iid", "source_branch")[:limit]
        )
