"""The only module that queries the watch columns of ``MergeRequestOwnership``. No platform I/O, no policy.

``Session.watch_*`` are no longer read or written; they are dropped one release after this one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce, Greatest

from sessions.inbox.ownership import aownership_for_branch
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, QuietReason

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

CI_KINDS = (EventKind.CI_FAILED, EventKind.CI_PASSED, EventKind.CI_STOPPED)


def latest_ci_results(session_id: str, merge_request_iid: int) -> QuerySet[InboxEvent]:
    """The latest CI event of each CI subject of a merge request in ``session_id``'s inbox, newest first."""
    ci = InboxEvent.objects.filter(session_id=session_id, merge_request_iid=merge_request_iid, kind__in=CI_KINDS)
    newest = ci.filter(subject=OuterRef("subject")).order_by("-created_at", "-pk").values("pk")[:1]
    return ci.filter(pk=Subquery(newest)).order_by("-created_at", "-pk")


def on_newest_commit(results: list[InboxEvent]) -> list[InboxEvent]:
    """Those of ``results`` (newest first) on the newest one's commit. Each was posted for its merge request's head,
    so the newest names the latest head, and a subject whose result is on an older commit has not run on it."""
    return [event for event in results if event.payload["sha"] == results[0].payload["sha"]] if results else []


def gave_up(session: OuterRef, *, since: datetime) -> Exists:
    """Whether CI on a merge request of ``session`` ran out of attempts since ``since`` and its subject has not
    passed after."""
    passed_after = InboxEvent.objects.filter(
        session=OuterRef("session"),
        merge_request_iid=OuterRef("merge_request_iid"),
        subject=OuterRef("subject"),
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
        return await aownership_for_branch(repo_id, ref)

    async def ahas_open(self, repo_id: str, ref: str) -> bool:
        return await MergeRequestOwnership.objects.filter(
            repo_id=repo_id, source_branch=ref, closed_at__isnull=True
        ).aexists()

    async def arecord_pipeline(self, ownership_id: int, pipeline_id: int, status: str) -> bool:
        """Record ``pipeline_id`` in ``status`` as judged, reporting whether this caller won: two deliveries of one
        pipeline must not both post its event. A retried pipeline keeps its id, so a new status is judged anew."""
        judged = Q(last_pipeline_id=pipeline_id, last_pipeline_status=status)
        updated = await (
            MergeRequestOwnership.objects
            .filter(pk=ownership_id)
            .filter(Q(last_pipeline_id__isnull=True) | ~judged)
            .aupdate(last_pipeline_id=pipeline_id, last_pipeline_status=status)
        )
        return updated == 1

    async def asuperseded(self, session_id: str | None, subject: str, sha: str, pipeline_id: int) -> bool:
        """Whether a pipeline newer than ``pipeline_id`` on ``sha`` was already judged for the CI ``subject``."""
        return await InboxEvent.objects.filter(
            session_id=session_id,
            subject=subject,
            kind__in=CI_KINDS,
            payload__sha=sha,
            payload__pipeline_id__gt=pipeline_id,
        ).aexists()

    async def ajudged(
        self, session_id: str | None, subject: str, pipeline_id: int, status: str, *, watch_on: bool
    ) -> bool:
        """Whether ``pipeline_id`` in ``status`` was already judged for the CI ``subject``. With the ``watch_on``, a
        result it kept quiet because it was off does not count, so a watch turned back on judges it anew."""
        judged = InboxEvent.objects.filter(
            session_id=session_id,
            subject=subject,
            kind__in=CI_KINDS,
            payload__pipeline_id=pipeline_id,
            payload__status=status,
        )
        if watch_on:
            judged = judged.exclude(reason=QuietReason.WATCH_OFF)
        return await judged.aexists()

    async def apassed(self, session_id: str, subject: str, pipeline_id: int, status: str) -> bool:
        """Whether the latest result posted for the CI ``subject`` is ``pipeline_id`` passing in ``status``."""
        latest = await (
            InboxEvent.objects
            .filter(session_id=session_id, subject=subject, kind__in=CI_KINDS)
            .order_by("-created_at", "-pk")
            .afirst()
        )
        return (
            latest is not None
            and latest.kind == EventKind.CI_PASSED
            and (latest.payload["pipeline_id"], latest.payload["status"]) == (pipeline_id, status)
        )

    async def aforget_pipeline(
        self, ownership_id: int, pipeline_id: int, status: str, *, previous: tuple[int | None, str]
    ) -> None:
        """Undo :meth:`arecord_pipeline` after posting the pipeline's result failed, unless a later one was recorded."""
        await MergeRequestOwnership.objects.filter(
            pk=ownership_id, last_pipeline_id=pipeline_id, last_pipeline_status=status
        ).aupdate(last_pipeline_id=previous[0], last_pipeline_status=previous[1])

    async def agave_up_before(self, event: InboxEvent) -> bool:
        """Whether an earlier pipeline on the commit of ``event``, a ``budget_spent`` CI failure, already ran out of
        attempts, so the owner was told."""
        return (
            await InboxEvent.objects
            .filter(
                session_id=event.session_id,
                merge_request_iid=event.merge_request_iid,
                kind=EventKind.CI_FAILED,
                reason=QuietReason.BUDGET_SPENT,
                payload__sha=event.payload["sha"],
            )
            .exclude(pk=event.pk)
            .aexists()
        )

    async def aset_enabled(self, ownership_id: int, *, enabled: bool) -> None:
        """Turn the watch on or off. Turning it on gives it its attempts back, as a person asking again is a new budget,
        and forgets the last pipeline judged so it is judged anew."""
        updates = {"watch_enabled": enabled}
        if enabled:
            updates |= {"attempts": 0, "last_pipeline_id": None, "last_pipeline_status": ""}
        await MergeRequestOwnership.objects.filter(pk=ownership_id).aupdate(**updates)

    def quiet(self, *, since: datetime, limit: int) -> QuerySet[MergeRequestOwnership, dict[str, Any]]:
        """Open, watched merge requests nothing was heard of since ``since``, at most ``limit``, stalest first: no CI
        result, whatever it was, no claim, which a publish makes, and no re-read by the reconciler
        (:meth:`amark_reconciled`). What a missed webhook may have left behind, a manual retry's pass included."""
        last_result = (
            InboxEvent.objects
            .filter(session=OuterRef("session"), merge_request_iid=OuterRef("merge_request_iid"), kind__in=CI_KINDS)
            .order_by("-created_at")
            .values("created_at")[:1]
        )
        heard_at = Greatest(
            "updated_at", Coalesce(Subquery(last_result), "updated_at"), Coalesce("last_reconciled_at", "updated_at")
        )
        return (
            MergeRequestOwnership.objects
            .filter(closed_at__isnull=True, watch_enabled=True)
            .annotate(heard_at=heard_at)
            .filter(heard_at__lt=since)
            .order_by("heard_at", "pk")
            .values("pk", "repo_id", "merge_request_iid", "source_branch")[:limit]
        )

    async def amark_reconciled(self, ownership_id: int, *, at: datetime) -> None:
        """Record that the reconciler re-read the merge request's CI at ``at``, which counts as hearing of it."""
        await MergeRequestOwnership.objects.filter(pk=ownership_id).aupdate(last_reconciled_at=at)
