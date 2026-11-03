"""Settling the events a run took once it ends, then starting the session's next run (research.md R12).

| Run outcome | Its taken events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | ``handled`` |
| FAILED after its owner asked it to stop | ``dismissed``: the owner chose to stop, so they are not retried |
| FAILED otherwise | back to ``pending`` with ``attempts + 1``, or ``failed`` (``retries_exhausted``) at
  ``MAX_EVENT_ATTEMPTS``, logged with ``logger.error`` |

A run that still holds its session's slot when it ends frees it: the drain claimed the slot when it created the run,
and only the run's task releases it, so a run stopped or failed before its task ran would keep it. The session is then
drained, except when the run's finish says ``skip_dispatch`` (the drain's own enqueue failed): draining there would
spend every attempt on the enqueue at once, so the sweep retries it instead, a minute apart.

A session with no DAIV user has no one to notify, so a run a CI failure woke posts its outcome on the merge request
instead (FR-018): its final message when it succeeded, its question with how to answer it when it asked one, and, once
CI failures no run could fix fail for good, the fixed note that DAIV could not address them, never the error itself.
A failed post is logged; the events keep their state. When a run a CI failure woke succeeded or asked a question
having pushed nothing, or the CI failure fails for good with none of the runs that tried it having pushed, and its
session was delegated, the parent session remembers it as a ``no_push`` ``delegated_ci_outcome`` (FR-021).

A run that took mentions and ended SUCCESSFUL or WAITING_INPUT answers them on their merge request (research.md R10):
its final message, its question with how to answer it, or, when it ended with nothing to say, the note that DAIV could
not address them, goes in the latest mention's thread, and each earlier mention's thread gets a line pointing there.
Mentions whose runs kept failing get that note in their threads once they fail for good. A failed post is logged; the
events keep their state.

The sweep (``sessions.tasks.drain_inboxes_cron_task``) settles events the same way when their run's slot went stale
before the run ended.

An inline chat turn takes its events under its holder id before its ``Run`` exists; when the turn ends before that,
:meth:`sessions.inbox.chat_claim.ChatClaim.arelease` puts them back as ``pending`` for the next turn, without spending
an attempt. A turn its owner stopped before DAIV answered is withdrawn (:func:`awithdraw_chat_run`): its ``Run`` is
deleted and its events go back the same way.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urldefrag

from django.db import transaction
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from asgiref.sync import sync_to_async

from codebase.base import GitPlatform
from core import ui_events
from core.constants import BOT_NAME
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.inbox.drain import Drain
from sessions.inbox.post import Inbox
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    Run,
    RunStatus,
    Session,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.db.models import QuerySet

    from codebase.clients import RepoClient

logger = logging.getLogger("daiv.sessions")

MAX_EVENT_ATTEMPTS = 3
ANSWERED_IN_ANOTHER_THREAD = "I answered this together with a later mention: {url}"


def on_run_ended(run: Run, *, drain: bool = True) -> None:
    """Settle the events ``run`` took, free the slot if ``run`` still holds it, then drain its session unless
    ``drain`` is ``False``. Never raises. SYNC ONLY."""
    try:
        settle_events(run)
    except Exception:
        logger.exception("inbox: settling the events of run=%s failed; the sweep retries them", run.pk)
    if not run.session_id:
        return
    try:
        SessionLock.release_for_run(run.session_id, run.pk)
    except Exception:
        logger.exception("inbox: could not free the slot run=%s held; it frees once stale", run.pk)
    if drain:
        Drain(run.session_id).start_logged()


aon_run_ended = sync_to_async(on_run_ended)


def withdraw_chat_run(run: Run) -> None:
    """Delete the chat ``Run`` its owner stopped before DAIV answered, putting back the events it took as ``pending``
    for the next turn: they never reached the model and cost no attempt. SYNC ONLY."""
    with transaction.atomic():
        InboxEvent.objects.filter(run=run, state=EventState.TAKEN).update(
            state=EventState.PENDING, run=None, updated_at=timezone.now()
        )
        Run.objects.filter(pk=run.pk).delete()
        transaction.on_commit(ui_events.publisher.runs_changed)


awithdraw_chat_run = sync_to_async(withdraw_chat_run)


def settle_events(run: Run) -> None:
    """Move the events ``run`` took to the state its outcome calls for. SYNC ONLY."""
    taken = InboxEvent.objects.filter(run=run, state=EventState.TAKEN)
    if run.status in (RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT):
        woken_by_ci = list(taken.filter(kind=EventKind.CI_FAILED, wake=True, merge_request_iid__isnull=False))
        mentions = list(
            taken.filter(kind=EventKind.MENTION, merge_request_iid__isnull=False).order_by("created_at", "id")
        )
        taken.update(state=EventState.HANDLED, updated_at=timezone.now())
        if woken_by_ci:
            _report_to_merge_requests(run, {event.merge_request_iid for event in woken_by_ci})
            _tell_parent_if_nothing_was_pushed(run, woken_by_ci)
        if mentions:
            _reply_to_mentions(run, mentions)
    elif run.status == RunStatus.FAILED and run.cancel_requested_at is not None:
        taken.update(state=EventState.DISMISSED, updated_at=timezone.now())
    elif run.status == RunStatus.FAILED:
        retry(taken, cause=f"run={run.pk} failed")


def _report_to_merge_requests(run: Run, merge_request_iids: set[int]) -> None:
    """Post ``run``'s reply (:func:`_reply`), addressed to no one, on the merge requests whose CI failure woke it, when
    its session has no DAIV user."""
    if not Session.objects.filter(pk=run.session_id, user__isnull=True).exists() or not run.response_text:
        return
    _comment_on_merge_requests(run.repo_id, merge_request_iids, lambda client: _reply(client, run, asker=""))


def _note_unfixed_ci_failures(failures: list[InboxEvent]) -> None:
    """Post the note that DAIV could not address them on the merge requests of CI failures no run could fix, when
    their session has no DAIV user."""
    ownerless = dict(
        Session.objects.filter(pk__in={event.session_id for event in failures}, user__isnull=True).values_list(
            "pk", "repo_id"
        )
    )
    by_repo: dict[str, set[int]] = {}
    for event in failures:
        if event.session_id in ownerless:
            by_repo.setdefault(ownerless[event.session_id], set()).add(event.merge_request_iid)
    for repo_id, iids in by_repo.items():
        _comment_on_merge_requests(repo_id, iids, _unable_note)


def _comment_on_merge_requests(
    repo_id: str, merge_request_iids: Iterable[int], body: Callable[[RepoClient], str]
) -> None:
    """Post ``body`` on each merge request; a failed post is logged and the next one is still tried."""
    from codebase.clients import RepoClient

    for iid in sorted(merge_request_iids):
        try:
            client = RepoClient.create_instance()
            client.create_merge_request_comment(repo_id, iid, body(client))
        except Exception:
            logger.exception("inbox: could not comment on %s!%s", repo_id, iid)


def _reply_to_mentions(run: Run, mentions: list[InboxEvent]) -> None:
    """Post ``run``'s answer in the thread of the latest mention on each merge request, then in each earlier mention's
    thread a line linking to that answer."""
    from codebase.clients import RepoClient

    by_merge_request: dict[int, list[InboxEvent]] = {}
    for event in mentions:
        by_merge_request.setdefault(event.merge_request_iid, []).append(event)
    for iid, events in by_merge_request.items():
        *earlier, latest = events
        try:
            client = RepoClient.create_instance()
            answer_id = _reply_in_thread(
                client, run.repo_id, latest, _answer(client, run, asker=latest.author_username)
            )
            pointer = ANSWERED_IN_ANOTHER_THREAD.format(url=_reply_url(client, latest, answer_id))
            for event in earlier:
                _reply_in_thread(client, run.repo_id, event, pointer)
        except Exception:
            logger.exception("inbox: could not reply to the mentions on %s!%s for run=%s", run.repo_id, iid, run.pk)


def _reply_url(client: RepoClient, mention: InboxEvent, comment_id: str | None) -> str:
    """The web address of the comment ``comment_id`` posted in ``mention``'s thread: the mention's own address with
    the comment's anchor, or the mention's address alone when the platform returned no id."""
    web_url = mention.payload["web_url"]
    if not comment_id:
        return web_url
    if client.git_platform == GitPlatform.GITLAB:
        anchor = f"note_{comment_id}"
    elif mention.payload.get("is_diff"):
        anchor = f"discussion_r{comment_id}"
    else:
        anchor = f"issuecomment-{comment_id}"
    return f"{urldefrag(web_url).url}#{anchor}"


def _answer(client: RepoClient, run: Run, *, asker: str) -> str:
    """``run``'s reply to ``asker`` (:func:`_reply`), or, when it said nothing, the note that DAIV could not address the
    request."""
    if not run.response_text:
        return _unable_note(client)
    return _reply(client, run, asker=asker)


def _reply(client: RepoClient, run: Run, *, asker: str) -> str:
    """``run``'s final message, or its question followed by how ``asker`` answers it: a reply mentioning DAIV. A blank
    ``asker`` leaves the question to whoever reads it."""
    from automation.agent.utils import repo_relative_text

    reply = repo_relative_text(run.response_text)
    if run.status != RunStatus.WAITING_INPUT:
        return reply
    footer = render_to_string(
        "core/ask_user_question.txt", {"asker": asker, "bot_username": client.current_user.username}
    )
    return f"{reply.rstrip()}\n\n{footer.strip()}"


def _unable_note(client: RepoClient) -> str:
    return render_to_string(
        "core/unable_address_review.txt",
        {
            "bot_name": BOT_NAME,
            "bot_username": client.current_user.username,
            "draft_published": False,
            "is_gitlab": client.git_platform == GitPlatform.GITLAB,
        },
    )


def _reply_in_thread(client: RepoClient, repo_id: str, mention: InboxEvent, body: str) -> str | None:
    """Post ``body`` in ``mention``'s thread; returns the id of the posted comment, when the platform gave one."""
    from codebase.clients.base import CommentThread

    thread = CommentThread(mention.payload["discussion_id"], on_diff=mention.payload.get("is_diff", False))
    return client.create_merge_request_comment(repo_id, mention.merge_request_iid, body, reply_to=thread)


def _tell_mentions_they_failed(mentions: list[InboxEvent]) -> None:
    """Post the note that DAIV could not address them in the threads of mentions no run could answer."""
    from codebase.clients import RepoClient

    repo_ids = dict(
        Session.objects.filter(pk__in={event.session_id for event in mentions}).values_list("pk", "repo_id")
    )
    for event in mentions:
        try:
            client = RepoClient.create_instance()
            _reply_in_thread(client, repo_ids[event.session_id], event, _unable_note(client))
        except Exception:
            logger.exception("inbox: could not tell mention event=%s that it failed", event.pk)


def _tell_parent_if_nothing_was_pushed(run: Run, woken_by_ci: list[InboxEvent]) -> None:
    """Remind the parent of a delegated session of each CI failure that woke ``run``, when ``run`` pushed nothing."""
    if Run.objects.filter(pk=run.pk).exclude(pushed_sha="").exists():
        return
    session = Session.objects.only("thread_id", "parent_id").get(pk=run.session_id)
    for event in woken_by_ci:
        _post_no_push(session, event)


def _tell_parents_nothing_was_pushed(failures: list[InboxEvent]) -> None:
    """Remind the parent of a delegated session of each CI failure no run could fix and none of the runs that tried it
    pushed for."""
    sessions = Session.objects.only("thread_id", "parent_id").in_bulk({event.session_id for event in failures})
    for event in failures:
        session = sessions.get(event.session_id)
        if session is None or session.parent_id is None:
            continue
        tried = {*event.payload.get("tried_runs", []), *([str(event.run_id)] if event.run_id else [])}
        if not Run.objects.filter(pk__in=tried).exclude(pushed_sha="").exists():
            _post_no_push(session, event)


def _post_no_push(session: Session, ci_failure: InboxEvent) -> None:
    Inbox.post_delegated_ci_outcome(
        session,
        merge_request_iid=ci_failure.merge_request_iid,
        pipeline_id=ci_failure.payload["pipeline_id"],
        outcome="no_push",
        web_url=ci_failure.payload.get("web_url", ""),
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
            if event.wake and event.run_id is not None:
                tried = [*event.payload.get("tried_runs", []), str(event.run_id)]
                event.payload = {**event.payload, "tried_runs": tried}
            event.state, event.run, event.claim_token = EventState.PENDING, None, ""
        event.save(update_fields=["attempts", "state", "failure", "run", "claim_token", "payload", "updated_at"])
    if exhausted:
        logger.error(
            "inbox: %d event(s) of thread_id=%s failed after %d attempts (%s): %s",
            len(exhausted),
            exhausted[0].session_id,
            MAX_EVENT_ATTEMPTS,
            cause,
            ", ".join(f"{event.kind}:{event.source_key}" for event in exhausted),
        )
        unfixed = [event for event in exhausted if event.kind == EventKind.CI_FAILED and event.wake]
        _forget_examined(unfixed)
        _note_unfixed_ci_failures([event for event in unfixed if event.merge_request_iid is not None])
        _tell_parents_nothing_was_pushed([event for event in unfixed if event.merge_request_iid is not None])
        _tell_mentions_they_failed([
            event for event in exhausted if event.kind == EventKind.MENTION and event.merge_request_iid is not None
        ])


def _forget_examined(failures: list[InboxEvent]) -> None:
    """Leave unexamined the commits of CI failures no run got to examine, so a re-run pipeline on one can wake again."""
    for event in failures:
        MergeRequestOwnership.objects.filter(
            repo_id__in=Session.objects.filter(pk=event.session_id).values("repo_id"),
            merge_request_iid=event.merge_request_iid,
            last_examined_sha=event.payload["sha"],
        ).update(last_examined_sha="")


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
