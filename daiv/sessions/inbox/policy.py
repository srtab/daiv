"""Whether a CI or delegation event may wake the agent when a run takes it (FR-031, FR-033).

A person's request (a chat message, a mention, a follow-up job) always wakes it. A CI failure or a finished delegated
batch does not while the session is muted, or once ``MAX_SYSTEM_RUNS_IN_A_ROW`` runs DAIV started itself followed each
other with no run a person started in between: the event is then only remembered, with the reason recorded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from sessions.inbox.kinds import GATED_KINDS
from sessions.models import QuietReason, Run, RunStatus, SessionOrigin

if TYPE_CHECKING:
    from sessions.models import Session

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
