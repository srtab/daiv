"""Putting a READY ``Run`` on the task queue, and what happens to the row when that does not land.

Every path that queues ``run_job_task`` for a run — a batch submit, a CI fix run, a QUEUED sibling released after
its predecessor finished, the orphan sweep — goes through :func:`aenqueue_run`, which builds the task's arguments
from the row, so each ``Run`` column reaches the agent the same way whichever path queued it. Webhook runs are not
among them: their callbacks enqueue their own tasks. A failed enqueue or link marks the row FAILED and emits
``run_finished`` (:func:`amark_failed_and_advance`), so the session's QUEUED siblings advance instead of waiting
behind a row that will never finish.

A QUEUED run whose ``waits_for`` run has not finished stays QUEUED, and so does every later run of its session: no
release path starts it early, and :func:`release_runs_waiting_for` starts it once that run ends. Whichever path
releases it, it starts from the branch the waited-for run's session is on by then, which that run may have moved
by publishing to a new branch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from asgiref.sync import async_to_sync

from sessions.executor.tasks import run_job_task
from sessions.models import Run, RunStatus, Session, SessionOrigin

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = logging.getLogger("daiv.sessions")

ENQUEUE_FAILED_PREFIX = "enqueue_failed"
# The agent may still push a commit or open an MR while the row shows FAILED.
LINK_FAILED_PREFIX = "link_failed (agent task will run but its result cannot be captured)"

MAX_CONSECUTIVE_DISPATCH_FAILURES = 3


@dataclass(frozen=True)
class EnqueueFailure:
    """:func:`aenqueue_run` could not get a run going; the row's ``error_message`` says why."""

    #: True when the task reached the queue but could not be linked to the row: the agent runs regardless.
    task_enqueued: bool


async def aenqueue_run(run: Run, *, log_context: str, skip_dispatch: bool = False) -> EnqueueFailure | None:
    """Enqueue ``run_job_task`` for a READY ``run`` and link its task result to the row.

    Returns ``None`` once both steps land. When either fails, :func:`amark_failed_and_advance` marks the row FAILED
    unless it moved on meanwhile, and sets ``run.error_message``, which starts with ``ENQUEUE_FAILED_PREFIX`` when no
    task reached the queue, or with ``LINK_FAILED_PREFIX`` when one did: that task still runs, and
    ``sessions.signals._sync_run_for_task`` no-ops on its result because no row links to it.
    """
    try:
        task = await run_job_task.aenqueue(
            repo_id=run.repo_id,
            prompt=run.prompt,
            thread_id=str(run.session_id),
            ref=run.ref or None,
            agent_model=run.agent_model or None,
            agent_thinking_level=run.agent_thinking_level or None,
            sandbox_environment_id=str(run.sandbox_environment_id) if run.sandbox_environment_id else None,
            run_id=str(run.pk),
            user_id=run.user_id,
            ask_user_enabled=await _acan_ask_user(run),
            fork_branch=run.trigger_type == SessionOrigin.DELEGATED_JOB,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("%s: enqueue failed for run=%s repo_id=%s", log_context, run.pk, run.repo_id)
        await amark_failed_and_advance(
            run, prefix=ENQUEUE_FAILED_PREFIX, err=err, log_context=log_context, skip_dispatch=skip_dispatch
        )
        return EnqueueFailure(task_enqueued=False)

    try:
        run.task_result_id = task.id
        await run.asave(update_fields=["task_result_id"])
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "%s: failed to link task_result_id=%s to run=%s (the task still runs)", log_context, task.id, run.pk
        )
        await amark_failed_and_advance(
            run, prefix=LINK_FAILED_PREFIX, err=err, log_context=log_context, skip_dispatch=skip_dispatch
        )
        return EnqueueFailure(task_enqueued=True)
    return None


async def _acan_ask_user(run: Run) -> bool:
    """Whether someone can answer a question ``run`` asks; a delegation's resume asks only where its session's origin
    could, since it continues the run that delegated."""
    trigger = run.trigger_type
    if trigger == SessionOrigin.DELEGATION_RESUME:
        trigger = await Session.objects.filter(pk=run.session_id).values_list("origin", flat=True).afirst()
    return trigger not in SessionOrigin.unattended()


async def amark_failed_and_advance(
    run: Run, *, prefix: str, err: Exception, log_context: str, skip_dispatch: bool = False
) -> None:
    """Fail a row its caller could not get running, through ``RunManager.afinish``, which emits ``run_finished``.

    The write lands only while the row still has the status the caller left it in, so a task that got running
    after all keeps its row. Does NOT touch ``Session.active_run_id`` (that is ``SessionLock``'s job); the
    "advance" is the ``run_finished`` emit, which dispatches the session's QUEUED siblings unless ``skip_dispatch``.
    ``log_context`` names the caller in the logs; ``run.error_message`` says why the run failed even when the write
    does not land.
    """
    expected_status = run.status
    now = timezone.now()
    run.error_message = f"{prefix}: {type(err).__name__}: {err}"
    try:
        failed = await Run.objects.afinish(
            run,
            RunStatus.FAILED,
            where=Q(status=expected_status),
            skip_dispatch=skip_dispatch,
            never_started=True,
            error_message=run.error_message,
            finished_at=now,
            started_at=run.started_at or now,
        )
    except Exception:
        logger.exception(
            "%s: failed to mark run=%s FAILED; it may be left %s, holding its session's slot so no QUEUED sibling "
            "runs — mark it FAILED by hand, then run release_orphan_queued_sessions",
            log_context,
            run.pk,
            expected_status,
        )
        return
    if not failed:
        logger.warning("%s: run=%s is no longer %s; left as it is", log_context, run.pk, expected_status)


def _claim_queued(run: Run) -> bool:
    """Move ``run`` from QUEUED to READY unless another dispatcher took it first, rebased on the branch its
    ``waits_for`` run left that run's session on.

    Raises ``IntegrityError`` when a peer already holds the session's active slot (``run_one_active_per_session``),
    from a savepoint of its own, so a caller inside a transaction can catch it and carry on.
    """
    with transaction.atomic():
        if Run.objects.filter(pk=run.pk, status=RunStatus.QUEUED).update(status=RunStatus.READY) != 1:
            return False
        if run.waits_for_id is not None:
            _rebase_on_waited_branch(run)
    run.refresh_from_db()
    return True


def _rebase_on_waited_branch(run: Run) -> None:
    branch = Run.objects.filter(pk=run.waits_for_id).values_list("session__ref", flat=True).first()
    if not branch:
        return
    Run.objects.filter(pk=run.pk).exclude(ref=branch).update(ref=branch)
    Session.objects.filter(pk=run.session_id).exclude(ref=branch).update(ref=branch)


def _enqueue_claimed(run: Run, *, log_context: str) -> bool:
    # skip_dispatch: dispatch_next_queued tries the next sibling itself; the orphan sweep leaves it for its next pass.
    return async_to_sync(aenqueue_run)(run, log_context=log_context, skip_dispatch=True) is None


def _still_waiting(run: Run) -> bool:
    """Whether ``run`` waits for a run that has not finished yet."""
    return (
        run.waits_for_id is not None
        and Run.objects.filter(pk=run.waits_for_id).exclude(status__in=RunStatus.terminal()).exists()
    )


def release_runs_waiting_for(run: Run) -> None:
    """Release the sessions whose oldest QUEUED run waited for ``run``, now that it has finished."""
    waiting = Run.objects.filter(waits_for=run, status=RunStatus.QUEUED).values_list("session_id", flat=True)
    for session_id in set(waiting):
        dispatch_next_queued(session_id)


def dispatch_next_queued(session_id: str) -> None:
    """Release the session's QUEUED runs, oldest first, until one is on the queue.

    Atomic compare-and-swap (``filter(pk=, status=QUEUED).update(status=READY)``) wins
    the race against concurrent dispatchers reading the same row. A losing race is
    retried on the next QUEUED row; a unique-constraint violation against a peer claim
    ends the pass. Enqueue or link failures mark the row FAILED (with ``finished_at`` set) and
    loop to the next sibling so a single bad row does not block the session — bounded
    by ``MAX_CONSECUTIVE_DISPATCH_FAILURES`` so a broker outage doesn't mass-fail the
    whole backlog.
    """
    consecutive_failures = 0
    while True:
        next_q = Run.objects.filter(session_id=session_id, status=RunStatus.QUEUED).order_by("created_at").first()
        if next_q is None or _still_waiting(next_q):
            return

        try:
            if not _claim_queued(next_q):
                continue
        except IntegrityError:
            # A concurrent insert (e.g. a fresh _submit_one) already created a
            # READY row on this session; the partial unique constraint blocks us.
            logger.debug("dispatch_next_queued: peer claim on session=%s, backing off", session_id)
            return

        if _enqueue_claimed(next_q, log_context="dispatch_next_queued"):
            return

        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_DISPATCH_FAILURES:
            logger.warning(
                "dispatch_next_queued: bailing on session=%s after %d consecutive dispatch failures; "
                "remaining QUEUED siblings left for release_orphan_queued_sessions",
                session_id,
                consecutive_failures,
            )
            return


@dataclass(frozen=True)
class OrphanRelease:
    released: int
    skipped: int
    errored: int


def release_orphan_queued_runs(session_ids: QuerySet | None = None) -> OrphanRelease:
    """Queue the oldest QUEUED run of every session that has no READY/RUNNING sibling; ``session_ids`` narrows the
    sweep to those sessions.

    Recovers the sessions :func:`dispatch_next_queued` gave up on after ``MAX_CONSECUTIVE_DISPATCH_FAILURES``,
    and the rare TOCTOU loss where the dispatcher missed a terminal transition or the row was created QUEUED but
    never picked up.
    """
    active_sessions = set(
        Run.objects.filter(status__in=[RunStatus.READY, RunStatus.RUNNING]).values_list("session_id", flat=True)
    )
    orphans = Run.objects.filter(status=RunStatus.QUEUED).filter(~Q(session_id__in=active_sessions))
    if session_ids is not None:
        orphans = orphans.filter(session_id__in=session_ids)
    orphans = orphans.order_by("session_id", "created_at")

    seen_sessions: set[str] = set()
    released = skipped = errored = 0
    for run in orphans.iterator():
        if run.session_id in seen_sessions:
            skipped += 1
            continue
        seen_sessions.add(run.session_id)
        if _still_waiting(run):
            skipped += 1
            continue
        try:
            claimed = _claim_queued(run)
        except IntegrityError:
            # A concurrent submission claimed the session between our snapshot of
            # active_sessions and this CAS; leave the row QUEUED for a future pass.
            claimed = False
        if not claimed:
            skipped += 1
            continue
        if _enqueue_claimed(run, log_context="release_orphan_queued_runs"):
            released += 1
        else:
            errored += 1
    return OrphanRelease(released=released, skipped=skipped, errored=errored)
