"""Whether a CI or delegation event may wake the agent when a run takes it (FR-031, FR-033).

A person's request (a chat message, a mention, a follow-up job) always wakes it. A CI failure or a finished delegated
batch does not while the session is muted, or once ``MAX_SYSTEM_RUNS_IN_A_ROW`` runs DAIV started itself followed each
other with no run a person started in between: the event is then only remembered, with the reason recorded, and the
session's owner is told once per streak (:func:`report_run_limit`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from sessions.inbox.kinds import GATED_KINDS
from sessions.models import InboxEvent, QuietReason, Run, RunStatus, Session, SessionOrigin
from sessions.signal_defs import inbox_paused

if TYPE_CHECKING:
    from datetime import datetime

MAX_SYSTEM_RUNS_IN_A_ROW = 3


def may_wake(session: Session, kind: str) -> QuietReason | None:
    """Why an event of ``kind`` may not wake ``session``'s agent, or ``None`` when it may. SYNC ONLY."""
    if kind not in GATED_KINDS:
        return None
    if session.inbox_muted:
        return QuietReason.MUTED
    if system_runs_in_a_row(session) >= MAX_SYSTEM_RUNS_IN_A_ROW:
        return QuietReason.RUN_LIMIT
    return None


amay_wake = sync_to_async(may_wake)


def system_runs_in_a_row(session: Session) -> int:
    """How many of ``session``'s latest runs DAIV started itself, counted back to the last one a person started.

    A QUEUED next run has not been dispatched, and its trigger type is provisional, so it does not count.
    """
    latest = (
        Run.objects
        .filter(session=session)
        .exclude(status=RunStatus.QUEUED)
        .order_by("-created_at")
        .values_list("trigger_type", flat=True)[:MAX_SYSTEM_RUNS_IN_A_ROW]
    )
    count = 0
    for trigger in latest:
        if trigger not in SessionOrigin.daiv_authored():
            break
        count += 1
    return count


def report_run_limit(session: Session) -> None:
    """Tell the receivers of ``inbox_paused`` that the limit blocked a wake on ``session``, naming the first event the
    current streak blocked, which is what keeps one streak to one notice. SYNC ONLY."""
    blocked = InboxEvent.objects.filter(session=session, reason=QuietReason.RUN_LIMIT)
    if (since := _last_person_run_at(session)) is not None:
        blocked = blocked.filter(created_at__gt=since)
    first = blocked.order_by("created_at", "id").first()
    if first is None:
        return
    inbox_paused.send_robust(
        sender=Session, session=session, first_blocked_event_id=str(first.pk), runs_in_a_row=MAX_SYSTEM_RUNS_IN_A_ROW
    )


areport_run_limit = sync_to_async(report_run_limit)


def _last_person_run_at(session: Session) -> datetime | None:
    return (
        Run.objects
        .filter(session=session)
        .exclude(status=RunStatus.QUEUED)
        .exclude(trigger_type__in=SessionOrigin.daiv_authored())
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
