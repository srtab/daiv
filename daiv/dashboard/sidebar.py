"""The rows the sidebar lists: the user's own sessions, and their schedules with each one's recent firings."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessions.models import RunStatus, Session

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet

    from accounts.models import User


SESSIONS_PAGE = 8
STATE_WORD_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.WAITING_INPUT, RunStatus.FAILED})

_SESSION_FIELDS = ("thread_id", "title", "last_active_at")


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


def _own_sessions(user: User) -> QuerySet[Session]:
    return (
        Session.objects
        .listed_for(user, mine=True)
        .filter(scheduled_job__isnull=True)
        .order_by("-last_active_at")
        .only(*_SESSION_FIELDS)
    )


def _session_row(session: Session, viewing: str) -> SessionRow:
    return SessionRow(
        thread_id=session.thread_id,
        title=session.title,
        status=session.latest_run_status or "",
        last_active_at=session.last_active_at,
        active=session.thread_id == viewing,
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
