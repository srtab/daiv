"""Settling the events a run took once it ends, then starting the session's next run (research.md R12).

| Run outcome | Its taken events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | ``handled`` |
| FAILED after its owner asked it to stop | ``dismissed``: the owner chose to stop, so they are not retried |
| FAILED otherwise | back to ``pending`` with ``attempts + 1``, or ``failed`` (``retries_exhausted``) at
  ``MAX_EVENT_ATTEMPTS``, logged with ``logger.error`` |

A session with no DAIV user has no one to notify, so a successful run a CI failure woke posts its summary on the
merge request instead (FR-018). A failed post is logged; the events stay handled. When such a run pushed nothing and
its session was delegated, the parent session remembers it as a ``no_push`` ``delegated_ci_outcome`` (FR-021).

A run that took mentions and ended SUCCESSFUL or WAITING_INPUT answers them on their merge request (research.md R10):
its final message, or its question, goes in the latest mention's thread, and each earlier mention's thread gets a line
pointing there. A failed post is logged; the events stay handled.

The sweep (``sessions.tasks.drain_inboxes_cron_task``) settles events the same way when their run's slot went stale
before the run ended.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async

from sessions.executor.lock import stale_cutoff
from sessions.inbox.drain import drain_logged
from sessions.inbox.post import post_delegated_ci_outcome
from sessions.models import EventFailure, EventKind, EventState, InboxEvent, Run, RunStatus, Session

if TYPE_CHECKING:
    from django.db.models import QuerySet
logger = logging.getLogger("daiv.sessions")

MAX_EVENT_ATTEMPTS = 3
ANSWERED_IN_ANOTHER_THREAD = "I answered this together with a later mention: {url}"


def on_run_ended(run: Run) -> None:
    """Settle the events ``run`` took, then drain its session. Never raises. SYNC ONLY."""
    try:
        settle_events(run)
    except Exception:
        logger.exception("inbox: settling the events of run=%s failed; the sweep retries them", run.pk)
    if run.session_id:
        drain_logged(run.session_id)


aon_run_ended = sync_to_async(on_run_ended)


def settle_events(run: Run) -> None:
    """Move the events ``run`` took to the state its outcome calls for. SYNC ONLY."""
    taken = InboxEvent.objects.filter(run=run, state=EventState.TAKEN)
    if run.status in (RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT):
        woken_by_ci = list(taken.filter(kind=EventKind.CI_FAILED, wake=True, merge_request_iid__isnull=False))
        mentions = list(
            taken.filter(kind=EventKind.MENTION, merge_request_iid__isnull=False).order_by("created_at", "id")
        )
        taken.update(state=EventState.HANDLED, updated_at=timezone.now())
        if woken_by_ci and run.status == RunStatus.SUCCESSFUL:
            _report_to_merge_requests(run, {event.merge_request_iid for event in woken_by_ci})
            _tell_parent_if_nothing_was_pushed(run, woken_by_ci)
        if mentions:
            _reply_to_mentions(run, mentions)
    elif run.status == RunStatus.FAILED and run.cancel_requested_at is not None:
        taken.update(state=EventState.DISMISSED, updated_at=timezone.now())
    elif run.status == RunStatus.FAILED:
        retry(taken, cause=f"run={run.pk} failed")


def _report_to_merge_requests(run: Run, merge_request_iids: set[int]) -> None:
    """Post ``run``'s summary on the merge requests whose CI failure woke it, when its session has no DAIV user."""
    from sessions.pipeline_watch.platform import WatchPlatform

    ownerless = Session.objects.filter(pk=run.session_id, user__isnull=True).exists()
    if not ownerless or not run.result_summary:
        return
    platform = WatchPlatform(run.repo_id)
    for iid in sorted(merge_request_iids):
        async_to_sync(platform.apost_note)(merge_request_iid=iid, body=run.result_summary)


def _reply_to_mentions(run: Run, mentions: list[InboxEvent]) -> None:
    """Post ``run``'s final message, or its question, in the thread of the latest mention on each merge request, and
    in each earlier mention's thread a line pointing there."""
    from automation.agent.utils import repo_relative_text
    from codebase.clients import RepoClient
    from codebase.clients.base import CommentThread

    if not (reply := run.response_text):
        return
    by_merge_request: dict[int, list[InboxEvent]] = {}
    for event in mentions:
        by_merge_request.setdefault(event.merge_request_iid, []).append(event)
    for iid, events in by_merge_request.items():
        *earlier, latest = events
        try:
            client = RepoClient.create_instance()
            for event, body in [
                (latest, repo_relative_text(reply)),
                *((event, ANSWERED_IN_ANOTHER_THREAD.format(url=latest.payload["web_url"])) for event in earlier),
            ]:
                thread = CommentThread(event.payload["discussion_id"], on_diff=event.payload.get("is_diff", False))
                client.create_merge_request_comment(run.repo_id, iid, body, reply_to=thread)
        except Exception:
            logger.exception("inbox: could not reply to the mentions on %s!%s for run=%s", run.repo_id, iid, run.pk)


def _tell_parent_if_nothing_was_pushed(run: Run, woken_by_ci: list[InboxEvent]) -> None:
    if Run.objects.filter(pk=run.pk).exclude(pushed_sha="").exists():
        return
    session = Session.objects.only("thread_id", "parent_id").get(pk=run.session_id)
    for event in woken_by_ci:
        post_delegated_ci_outcome(
            session,
            merge_request_iid=event.merge_request_iid,
            pipeline_id=event.payload["pipeline_id"],
            outcome="no_push",
            web_url=event.payload.get("web_url", ""),
        )


def retry(events: QuerySet[InboxEvent], *, cause: str) -> None:
    """Put ``events`` back for the next run, or fail the ones out of attempts. SYNC ONLY."""
    exhausted: list[InboxEvent] = []
    for event in events:
        event.attempts += 1
        if event.attempts >= MAX_EVENT_ATTEMPTS:
            event.state, event.failure = EventState.FAILED, EventFailure.RETRIES_EXHAUSTED
            exhausted.append(event)
        else:
            event.state, event.run, event.claim_token = EventState.PENDING, None, ""
        event.save(update_fields=["attempts", "state", "failure", "run", "claim_token", "updated_at"])
    if exhausted:
        logger.error(
            "inbox: %d event(s) of thread_id=%s failed after %d attempts (%s): %s",
            len(exhausted),
            exhausted[0].session_id,
            MAX_EVENT_ATTEMPTS,
            cause,
            ", ".join(f"{event.kind}:{event.source_key}" for event in exhausted),
        )


def recover_stale_events() -> int:
    """Settle the events taken on sessions whose slot went stale: by their run's outcome when it ended, else as a
    failed attempt, since their run will never end. Returns how many events moved. SYNC ONLY."""
    stale = InboxEvent.objects.filter(state=EventState.TAKEN, session__last_active_at__lt=stale_cutoff())
    moved = 0
    for run in Run.objects.filter(pk__in=stale.filter(run__status__in=RunStatus.terminal()).values("run_id")):
        moved += InboxEvent.objects.filter(run=run, state=EventState.TAKEN).count()
        settle_events(run)
    orphaned = stale.filter(Q(run__isnull=True) | ~Q(run__status__in=RunStatus.terminal()))
    for thread_id in set(orphaned.values_list("session_id", flat=True)):
        events = orphaned.filter(session_id=thread_id)
        moved += events.count()
        retry(events, cause="its run's slot went stale before the run ended")
    return moved
