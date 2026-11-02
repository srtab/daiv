"""What a session will do next, for the "Up next" section that ends its page.

``groups`` are the runs still to start, in the order they will: a run the drain promoted that no worker started yet
(``starting``), then the job runs ``sessions.queue`` holds, then one per branch and requester for the pending events
that will wake a run, ordered by their oldest event as the drain takes them. Each group holds the bubbles of the
requests that start it and the remembered events its run will be told. ``told`` holds the remembered events no group
will take, one block per branch. What the drain would decide for a pending event is shown now: an event a newer one
of its subject replaces is left out, and a waking event the policy quiets is a remembered line with its reason.

Runs the page already shows as waiting (``SessionActivity.waiting``) are left out, so nothing shows twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import Q

from asgiref.sync import sync_to_async

from sessions.executor.lock import session_activity
from sessions.inbox.drain import Requester, next_runs, requester_of
from sessions.inbox.kinds import PERSON_KINDS
from sessions.inbox.policy import quiet_reason_now
from sessions.inbox.render import author_name
from sessions.inbox_display import bubble, run_bubble
from sessions.models import EventState, InboxEvent, RunStatus
from sessions.transcript import inbox_event_entry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from accounts.models import User
    from sessions.models import Run, Session


def up_next_payload(session: Session, runs: Sequence[Run], viewer: User) -> dict[str, Any] | None:
    """``session``'s Up next for ``viewer``, from its chronologically ordered ``runs``; ``None`` when nothing is
    pending. SYNC ONLY."""
    waiting = set(session_activity(session, runs).waiting)
    starting = [run for run in runs if run.status == RunStatus.READY and run not in waiting]
    inbox_runs = list(next_runs(session).select_related("user"))
    inbox_ids = {run.pk for run in inbox_runs}
    jobs = [run for run in runs if run.status == RunStatus.QUEUED and run.pk not in inbox_ids and run not in waiting]
    events = list(
        InboxEvent.objects
        .filter(Q(state=EventState.PENDING) | Q(state=EventState.TAKEN, run__in=starting), session=session)
        .select_related("author")
        .order_by("created_at", "id")
    )

    groups = [_starting_group(run, events, viewer) for run in starting]
    groups.extend(_group(run, run.user, viewer, bubbles=[run_bubble(run)]) for run in jobs)

    by_id = {run.pk: run for run in inbox_runs}
    by_key: dict[tuple[str, Requester], Run] = {}
    for run in inbox_runs:
        by_key.setdefault((run.ref, Requester.of_run(run)), run)
    asks: dict[Any, tuple[str, Requester, list[InboxEvent]]] = {}
    remembered: list[tuple[InboxEvent, str | None]] = []
    for event in _survivors([event for event in events if event.state == EventState.PENDING]):
        reason = quiet_reason_now(session, event)
        if not event.wake or reason is not None:
            remembered.append((event, reason))
            continue
        requester = requester_of(event, session)
        run = by_id.get(event.run_id) or by_key.get((event.ref, requester))
        key = run.pk if run is not None else (event.ref, requester)
        asks.setdefault(key, (run.ref if run is not None else event.ref, requester, []))[2].append(event)
    inbox_groups = []
    for key, (ref, requester, asked) in asks.items():
        run = by_id.get(key)
        user = run.user if run is not None else _requester_user(session, asked, requester)
        inbox_groups.append((ref, _group(run, user, viewer, bubbles=[bubble(event) for event in asked], asked=asked)))
    groups.extend(group for _, group in inbox_groups)

    told: dict[str, list[dict[str, Any]]] = {}
    for event, reason in remembered:
        entry = inbox_event_entry(event, reason=reason)
        group = next((group for ref, group in inbox_groups if event.ref in ("", ref)), None)
        if group is not None:
            group["told"].append(entry)
        else:
            told.setdefault("" if event.ref in ("", session.ref) else event.ref, []).append(entry)

    if not groups and not told:
        return None
    return {"groups": groups, "told": [{"branch": branch, "lines": lines} for branch, lines in told.items()]}


aup_next_payload = sync_to_async(up_next_payload)


def _starting_group(run: Run, events: list[InboxEvent], viewer: User) -> dict[str, Any]:
    taken = [event for event in events if event.run_id == run.pk and event.state == EventState.TAKEN]
    group = _group(
        run,
        run.user,
        viewer,
        bubbles=[bubble(event) for event in taken if event.wake] or [run_bubble(run)],
        asked=taken,
        starting=True,
    )
    group["told"] = [inbox_event_entry(event) for event in taken if not event.wake]
    return group


def _group(
    run: Run | None,
    user: User | None,
    viewer: User,
    *,
    bubbles: list[dict[str, Any]],
    asked: Sequence[InboxEvent] = (),
    starting: bool = False,
) -> dict[str, Any]:
    person = next((event for event in asked if event.kind in PERSON_KINDS), None)
    return {
        "run_id": str(run.pk) if run is not None else "",
        "who": (user.name or user.email) if user is not None else (author_name(person) if person else ""),
        "is_viewer": user is not None and user.pk == viewer.pk,
        "starting": starting,
        "bubbles": bubbles,
        "told": [],
    }


def _survivors(pending: list[InboxEvent]) -> list[InboxEvent]:
    """``pending`` less the events the drain would supersede: all but the newest of each subject."""
    newest = {event.subject: event for event in pending if event.subject}
    return [event for event in pending if not event.subject or newest[event.subject] is event]


def _requester_user(session: Session, asked: list[InboxEvent], requester: Requester) -> User | None:
    if requester.user_id is None:
        return None
    return next((event.author for event in asked if event.author_id == requester.user_id), session.user)
