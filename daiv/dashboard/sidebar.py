"""The rows the sidebar lists: the user's own sessions, and their schedules with each one's recent firings."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.db.models import Case, F, Max, Min, Q, QuerySet, Value, When, Window
from django.db.models.functions import RowNumber
from django.urls import reverse
from django.utils import timezone

from sessions.models import Run, RunStatus, Session, SessionOrigin

from schedules.models import ScheduledJob

if TYPE_CHECKING:
    from accounts.models import User


SESSIONS_PAGE = 8
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

_SESSION_FIELDS = ("thread_id", "title", "last_active_at", "latest_run_status")


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
    sessions: tuple[SessionRow, ...] = ()
    has_more: bool = False
    schedules: tuple[ScheduleRow, ...] = ()


def _own_sessions(user: User) -> QuerySet[Session, dict[str, Any]]:
    return (
        Session.objects
        .listed_for(user, mine=True)
        .filter(scheduled_job__isnull=True)
        .order_by("-last_active_at")
        .values(*_SESSION_FIELDS)
    )


def _session_row(session: dict[str, Any], viewing: str) -> SessionRow:
    return SessionRow(
        thread_id=session["thread_id"],
        title=session["title"],
        status=session["latest_run_status"] or "",
        last_active_at=session["last_active_at"],
        active=session["thread_id"] == viewing,
    )


def session_rows(user: User, *, limit: int = SESSIONS_PAGE, viewing: str = "") -> tuple[tuple[SessionRow, ...], bool]:
    """The user's own sessions outside any schedule, newest first, and whether more follow.

    The session being viewed is appended when it is theirs but falls outside the page.
    """
    sessions = list(_own_sessions(user)[: limit + 1])
    has_more = len(sessions) > limit
    rows = [_session_row(session, viewing) for session in sessions[:limit]]
    if viewing and not any(row.active for row in rows):
        rows.extend(_session_row(session, viewing) for session in _own_sessions(user).filter(thread_id=viewing))
    return tuple(rows), has_more


def _listed_schedules(user: User, now: datetime) -> QuerySet[ScheduledJob]:
    theirs = Q(user=user) | Q(pk__in=ScheduledJob.objects.filter(subscribers=user).values("pk"))
    current = Q(is_enabled=True) | Q(last_run_at__gte=now - FIRED_WITHIN)
    return (
        ScheduledJob.objects
        .filter(theirs & current)
        .order_by(F("next_run_at").asc(nulls_last=True), F("last_run_at").desc(nulls_last=True), "pk")
        .only("pk", "name", "next_run_at")[:SCHEDULES_LIMIT]
    )


def _newest_firings(user: User, schedule_ids: list[int], viewing: str) -> list[dict[str, Any]]:
    """Each schedule's newest firings, plus the one holding the viewed session wherever it falls."""
    holds_viewed = Max(Case(When(session_id=viewing, then=Value(1)), default=Value(0)))
    return list(
        Run.objects
        .filter(
            trigger_type=SessionOrigin.SCHEDULE,
            batch_id__isnull=False,
            session__scheduled_job_id__in=schedule_ids,
            session__in=Session.objects.listed_for(user, mine=True).values("pk"),
        )
        .order_by()
        .values("batch_id", schedule_id=F("session__scheduled_job_id"))
        .annotate(fired_at=Min("created_at"), holds_viewed=holds_viewed)
        .annotate(
            rank=Window(
                RowNumber(),
                partition_by=F("session__scheduled_job_id"),
                order_by=(F("holds_viewed").desc(), F("fired_at").desc()),
            )
        )
        .filter(rank__lte=FIRINGS_PER_SCHEDULE + 1)
    )


def _firing_sessions(user: User, batch_ids: list) -> dict[Any, list[tuple[str, str]]]:
    rows = (
        Session.objects
        .listed_for(user, mine=True)
        .filter(runs__batch_id__in=batch_ids, runs__trigger_type=SessionOrigin.SCHEDULE)
        .values_list("runs__batch_id", "thread_id", "latest_run_status")
    )
    sessions: dict[Any, list[tuple[str, str]]] = defaultdict(list)
    for batch_id, thread_id, status in rows:
        sessions[batch_id].append((thread_id, status or ""))
    return sessions


def _worst(statuses: list[str]) -> str:
    return next((status for status in WORST_FIRST if status in statuses), "")


def _firing_row(firing: dict[str, Any], sessions: list[tuple[str, str]]) -> FiringRow:
    if len(sessions) == 1:
        url = reverse("session_detail", kwargs={"thread_id": sessions[0][0]})
    else:
        url = reverse("session_list") + f"?batch={firing['batch_id']}"
    return FiringRow(
        fired_at=firing["fired_at"],
        status=_worst([status for _, status in sessions]),
        repo_count=len(sessions),
        url=url,
        active=bool(firing["holds_viewed"]),
    )


def _shown_firings(firings: list[FiringRow]) -> tuple[FiringRow, ...]:
    newest = sorted(firings, key=lambda firing: firing.fired_at, reverse=True)
    shown = newest[:FIRINGS_PER_SCHEDULE]
    shown.extend(firing for firing in newest[FIRINGS_PER_SCHEDULE:] if firing.active)
    return tuple(shown)


def _schedule_row(schedule: ScheduledJob, firings: tuple[FiringRow, ...]) -> ScheduleRow:
    latest = firings[0].status if firings else ""
    return ScheduleRow(
        pk=schedule.pk,
        name=schedule.name,
        next_run_at=schedule.next_run_at,
        status=latest if latest in STATE_WORD_STATUSES else "",
        firings=firings,
        starts_open=latest in _OPENS_SCHEDULE or any(firing.active for firing in firings),
        all_runs_url=reverse("session_list") + f"?schedule={schedule.pk}",
    )


def schedule_rows(user: User, *, viewing: str = "", now: datetime | None = None) -> tuple[ScheduleRow, ...]:
    """The schedules the user owns or subscribes to that are enabled or fired lately, each with its newest firings.

    A firing is one dispatch: the sessions its schedule runs share a ``batch_id``.
    """
    schedules = list(_listed_schedules(user, now or timezone.now()))
    if not schedules:
        return ()
    firings = _newest_firings(user, [schedule.pk for schedule in schedules], viewing)
    sessions = _firing_sessions(user, [firing["batch_id"] for firing in firings]) if firings else {}
    by_schedule: dict[int, list[FiringRow]] = defaultdict(list)
    for firing in firings:
        by_schedule[firing["schedule_id"]].append(_firing_row(firing, sessions[firing["batch_id"]]))
    return tuple(_schedule_row(schedule, _shown_firings(by_schedule[schedule.pk])) for schedule in schedules)


def sidebar_lists(user: User, *, limit: int = SESSIONS_PAGE, viewing: str = "") -> SidebarLists:
    sessions, has_more = session_rows(user, limit=limit, viewing=viewing)
    return SidebarLists(sessions=sessions, has_more=has_more, schedules=schedule_rows(user, viewing=viewing))
