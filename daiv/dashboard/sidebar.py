"""The rows the sidebar lists: the user's own sessions, and their schedules with each one's recent firings."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import reduce
from operator import attrgetter, or_
from typing import TYPE_CHECKING, Any

from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.urls import reverse
from django.utils import timezone

from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.validators import MAX_REPOS_PER_BATCH

from schedules.models import ScheduledJob

if TYPE_CHECKING:
    from accounts.models import User


SESSIONS_PAGE = 8
SESSIONS_MAX = 80
STATE_WORD_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.FAILED})
SCHEDULES_LIMIT = 10
FIRINGS_PER_SCHEDULE = 3
FIRED_WITHIN = timedelta(days=14)
WORST_FIRST = (
    RunStatus.FAILED,
    RunStatus.WAITING_INPUT,
    RunStatus.RUNNING,
    RunStatus.QUEUED,
    RunStatus.READY,
    RunStatus.SUCCESSFUL,
)
_OPENS_SCHEDULE = frozenset({RunStatus.FAILED, RunStatus.WAITING_INPUT})
NEWEST_SESSIONS = FIRINGS_PER_SCHEDULE * MAX_REPOS_PER_BATCH

_SESSION_FIELDS = ("thread_id", "title", "last_active_at", "latest_run_status")
_FIRING_FIELDS = ("scheduled_job_id", "runs__batch_id", "runs__created_at", "thread_id", "latest_run_status")


@dataclass(frozen=True, slots=True)
class SessionRow:
    thread_id: str
    title: str
    status: str
    last_active_at: datetime
    active: bool

    @property
    def shows_state(self) -> bool:
        return self.status in STATE_WORD_STATUSES


@dataclass(frozen=True, slots=True)
class FiringRow:
    batch_id: str
    fired_at: datetime
    status: str
    repo_count: int
    url: str
    active: bool


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    pk: int
    name: str
    next_run_at: datetime | None
    status: str
    firings: tuple[FiringRow, ...]
    starts_open: bool
    all_runs_url: str


@dataclass(frozen=True, slots=True)
class SidebarLists:
    """The rows both lists show. ``failed`` means they could not be read, so their emptiness says nothing."""

    sessions: tuple[SessionRow, ...] = ()
    has_more: bool = False
    schedules: tuple[ScheduleRow, ...] = ()
    limit: int = SESSIONS_PAGE
    failed: bool = False

    @property
    def next_limit(self) -> int | None:
        """The depth "Show more" asks for next, or ``None`` at the cap, where it links to the sessions page instead."""
        return min(self.limit + SESSIONS_PAGE, SESSIONS_MAX) if self.limit < SESSIONS_MAX else None


@dataclass(slots=True)
class _Firing:
    """One dispatch of a schedule, gathered from the rows of the sessions it started."""

    batch_id: Any
    schedule_id: int
    fired_at: datetime
    statuses: dict[str, str] = field(default_factory=dict)

    def add(self, thread_id: str, status: str, created_at: datetime) -> None:
        self.statuses[thread_id] = status
        self.fired_at = min(self.fired_at, created_at)

    def row(self, viewing: str) -> FiringRow:
        if len(self.statuses) == 1:
            url = reverse("session_detail", kwargs={"thread_id": next(iter(self.statuses))})
        else:
            url = reverse("session_list") + f"?batch={self.batch_id}"
        statuses = set(self.statuses.values())
        return FiringRow(
            batch_id=str(self.batch_id),
            fired_at=self.fired_at,
            status=next((status for status in WORST_FIRST if status in statuses), ""),
            repo_count=len(self.statuses),
            url=url,
            active=viewing in self.statuses,
        )


class Sidebar:
    """The sidebar's lists for ``user``, marking ``viewing``, the session their page shows, wherever it falls."""

    def __init__(self, user: User, *, viewing: str = "", now: datetime | None = None) -> None:
        self.user = user
        self.viewing = viewing
        self.now = now or timezone.now()

    def lists(self, *, limit: int = SESSIONS_PAGE) -> SidebarLists:
        """Both lists, with ``limit`` sessions clamped to between one page and ``SESSIONS_MAX``."""
        limit = min(max(limit, SESSIONS_PAGE), SESSIONS_MAX)
        sessions, has_more = self.session_rows(limit=limit)
        return SidebarLists(sessions=sessions, has_more=has_more, schedules=self.schedule_rows(), limit=limit)

    def session_rows(self, *, limit: int = SESSIONS_PAGE) -> tuple[tuple[SessionRow, ...], bool]:
        """The user's own sessions outside any schedule, newest first, and whether more follow.

        The session being viewed is appended when it is theirs but falls outside the page.
        """
        sessions = list(self._own_sessions()[: limit + 1])
        has_more = len(sessions) > limit
        rows = [self._session_row(session) for session in sessions[:limit]]
        if self.viewing and not any(row.active for row in rows):
            rows.extend(self._session_row(session) for session in self._own_sessions().filter(thread_id=self.viewing))
        return tuple(rows), has_more

    def schedule_rows(self) -> tuple[ScheduleRow, ...]:
        """The schedules the user owns or subscribes to that are enabled or fired lately, each with its newest firings.

        A firing is one dispatch: the sessions its schedule runs share a ``batch_id``.
        """
        schedules = self._schedules()
        if not schedules:
            return ()
        firings = self._firings(schedules)
        return tuple(self._schedule_row(schedule, firings.get(schedule["pk"], [])) for schedule in schedules)

    def _own_sessions(self) -> QuerySet[Session, dict[str, Any]]:
        return (
            Session.objects
            .listed_for(self.user, mine=True)
            .filter(scheduled_job__isnull=True)
            .order_by("-last_active_at")
            .values(*_SESSION_FIELDS)
        )

    def _session_row(self, session: dict[str, Any]) -> SessionRow:
        return SessionRow(
            thread_id=session["thread_id"],
            title=session["title"],
            status=session["latest_run_status"] or "",
            last_active_at=session["last_active_at"],
            active=session["thread_id"] == self.viewing,
        )

    def _schedules(self) -> list[dict[str, Any]]:
        schedules = (
            ScheduledJob.objects
            .followed_by(self.user)
            .current(since=self.now - FIRED_WITHIN)
            .with_newest_sessions_since(NEWEST_SESSIONS)
        )
        fields = ["pk", "name", "next_run_at", "newest_sessions_since"]
        if self.viewing:
            viewed = Run.objects.filter(
                session_id=self.viewing,
                session__scheduled_job=OuterRef("pk"),
                trigger_type=SessionOrigin.SCHEDULE,
                batch_id__isnull=False,
            )
            schedules = schedules.annotate(viewed_batch=Subquery(viewed.values("batch_id")[:1]))
            fields.append("viewed_batch")
        ordered = schedules.order_by(
            F("next_run_at").asc(nulls_last=True), F("last_run_at").desc(nulls_last=True), "pk"
        )
        return list(ordered.values(*fields)[:SCHEDULES_LIMIT])

    def _firings(self, schedules: list[dict[str, Any]]) -> dict[int, list[_Firing]]:
        """Each schedule's newest firings, newest first, then the one holding the viewed session wherever it falls.

        A firing starts one session per repository, so each schedule's ``NEWEST_SESSIONS`` newest sessions reach its
        newest firings; they are picked from those alone, so the cost stays flat as history grows, then read whole.
        """
        picked = self._newest_batches(reduce(or_, (self._newest_sessions_of(schedule) for schedule in schedules)))
        newest = [batch_id for batch_ids in picked.values() for batch_id in batch_ids]
        viewed = next((schedule["viewed_batch"] for schedule in schedules if schedule.get("viewed_batch")), None)
        extra = [viewed] if viewed is not None and viewed not in newest else []
        if not newest and not extra:
            return {}
        read = self._gather([*newest, *extra])
        firings = {
            schedule_id: sorted(
                (read[batch_id] for batch_id in batch_ids if batch_id in read), key=attrgetter("fired_at"), reverse=True
            )
            for schedule_id, batch_ids in picked.items()
        }
        for firing in (read[batch_id] for batch_id in extra if batch_id in read):
            firings.setdefault(firing.schedule_id, []).append(firing)
        return firings

    @staticmethod
    def _newest_sessions_of(schedule: dict[str, Any]) -> Q:
        newest = Q(scheduled_job_id=schedule["pk"], archived_at__isnull=True)
        since = schedule["newest_sessions_since"]
        return newest & Q(created_at__gte=since) if since is not None else newest

    def _newest_batches(self, where: Q) -> dict[int, list[Any]]:
        """The ``batch_id`` of each schedule's newest firings among the user's sessions ``where`` selects, newest first.

        Each session's batch is a lookup of its own, not a join, which Postgres would serve by reading every run.
        """
        batch = Run.objects.filter(
            session=OuterRef("pk"), trigger_type=SessionOrigin.SCHEDULE, batch_id__isnull=False
        ).order_by("created_at")
        rows = (
            Session.objects
            .owned_by(self.user)
            .filter(where)
            .order_by()
            .annotate(schedule_batch=Subquery(batch.values("batch_id")[:1]))
            .values_list("scheduled_job_id", "schedule_batch", "created_at")
        )
        fired: dict[int, dict[Any, datetime]] = defaultdict(dict)
        for schedule_id, batch_id, created_at in rows:
            if batch_id is not None:
                fired[schedule_id][batch_id] = min(created_at, fired[schedule_id].get(batch_id, created_at))
        return {
            schedule_id: sorted(batches, key=batches.__getitem__, reverse=True)[:FIRINGS_PER_SCHEDULE]
            for schedule_id, batches in fired.items()
        }

    def _gather(self, batch_ids: list[Any]) -> dict[Any, _Firing]:
        """The firings ``batch_ids`` name, whole, from the sessions the user's lists show."""
        rows = (
            Session.objects
            .listed_for(self.user, mine=True)
            .filter(runs__batch_id__in=batch_ids, runs__trigger_type=SessionOrigin.SCHEDULE)
            .order_by()
            .values_list(*_FIRING_FIELDS)
        )
        firings: dict[Any, _Firing] = {}
        for schedule_id, batch_id, created_at, thread_id, status in rows:
            firing = firings.setdefault(batch_id, _Firing(batch_id, schedule_id, created_at))
            firing.add(thread_id, status or "", created_at)
        return firings

    def _schedule_row(self, schedule: dict[str, Any], firings: list[_Firing]) -> ScheduleRow:
        rows = tuple(firing.row(self.viewing) for firing in firings)
        latest = rows[0].status if rows else ""
        return ScheduleRow(
            pk=schedule["pk"],
            name=schedule["name"],
            next_run_at=schedule["next_run_at"],
            status=latest if latest in STATE_WORD_STATUSES else "",
            firings=rows,
            starts_open=latest in _OPENS_SCHEDULE or any(row.active for row in rows),
            all_runs_url=reverse("session_list") + f"?schedule={schedule['pk']}",
        )
