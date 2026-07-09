from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver

from asgiref.sync import async_to_sync
from django_tasks.signals import task_finished, task_started
from jobs.tasks import run_job_task

logger = logging.getLogger("daiv.sessions")

# Emitted when a Run transitions to a terminal status (SUCCESSFUL or FAILED).
# Arguments: run (Run instance).
run_finished = Signal()

# ``error_message`` prefix for the orphan-task failure mode: the broker holds a task
# that couldn't be linked back to its Run row, so the agent may run to completion
# (push a commit / open an MR) while the row shows FAILED. Shared by both the
# batch-submit path (services) and the dispatcher (signals) so the sentinel an
# operator greps for stays identical across them.
LINK_FAILED_PREFIX = "link_failed (agent task will run but its result cannot be captured)"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def backfill_session_user(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Link orphaned runs and sessions to a newly created user by matching external_username.

    Only runs on user creation, not updates — renaming a user will not re-trigger backfill.
    Errors are caught so that a problem in session backfill never breaks user creation.
    """
    if not created:
        return

    from sessions.models import Run, Session

    try:
        updated_runs = Run.objects.filter(user__isnull=True, external_username=instance.username).update(user=instance)
    except Exception:
        logger.exception("Failed to backfill runs for new user %s (pk=%s)", instance.username, instance.pk)
        updated_runs = 0

    if updated_runs:
        logger.info("Backfilled %d runs for new user %s (pk=%s)", updated_runs, instance.username, instance.pk)

    try:
        updated_sessions = Session.objects.filter(user__isnull=True, external_username=instance.username).update(
            user=instance
        )
    except Exception:
        logger.exception("Failed to backfill sessions for new user %s (pk=%s)", instance.username, instance.pk)
        return

    if updated_sessions:
        logger.info("Backfilled %d sessions for new user %s (pk=%s)", updated_sessions, instance.username, instance.pk)


def emit_run_finished_if_terminal(run: Any, previous_status: str | None, *, skip_dispatch: bool = False) -> None:
    """Emit run_finished if the run just transitioned to a terminal status.

    ``skip_dispatch`` is forwarded to receivers; the in-session dispatcher uses it to
    suppress recursive re-entry while still letting notification receivers fire.
    """
    from sessions.models import RunStatus

    if run.status not in RunStatus.terminal():
        return
    if previous_status in RunStatus.terminal():
        return  # Already emitted on a prior save
    results = run_finished.send_robust(sender=type(run), run=run, skip_dispatch=skip_dispatch)
    for recv, response in results:
        if isinstance(response, Exception):
            logger.error(
                "Receiver %s failed for run_finished (run=%s)",
                getattr(recv, "__name__", recv),
                run.pk,
                exc_info=response,
            )


def _sync_run_for_task(task_result_id: Any) -> None:
    """Pull latest status/timing/result from the linked DBTaskResult into the Run row.

    Silently no-ops if no Run is linked to the given task_result_id (e.g. tasks that
    don't create a Run, or the brief cross-process race where ``task_started`` fires
    before the Run row is committed on the web side — ``task_finished`` will catch up).
    Errors are swallowed and logged so the worker loop is never crashed by sync failures.
    """
    from sessions.models import Run

    try:
        run = (
            Run.objects
            .select_related("task_result", "session", "session__scheduled_job", "session__scheduled_job__user", "user")
            .filter(task_result_id=task_result_id)
            .first()
        )
        if run is None:
            return
        run.sync_and_save()
    except Exception:
        logger.exception("Failed to sync run for task_result_id=%s", task_result_id)


@receiver([task_started, task_finished])
def sync_run_on_task_signal(sender: type, task_result: Any, **kwargs: Any) -> None:
    """Sync the linked Run on task state transitions (RUNNING / terminal).

    The django-tasks worker commits DBTaskResult state (via ``claim``/``set_successful``/
    ``set_failed``) before dispatching these signals, so reading back the row here is safe
    without a transaction guard.
    """
    _sync_run_for_task(task_result.id)


#: Cap on consecutive enqueue failures before the dispatcher bails. A persistent
#: broker outage would otherwise mass-fail every QUEUED row on the session within
#: a single signal-handler call; bailing leaves the rest QUEUED for
#: ``release_orphan_queued_sessions`` to recover when the broker is back.
MAX_CONSECUTIVE_DISPATCH_FAILURES = 3


@receiver(run_finished)
def dispatch_next_in_session(sender: type, run: Any, **kwargs: Any) -> None:
    """Release queued continuations on this session, one at a time, until one succeeds.

    Atomic compare-and-swap (``filter(pk=, status=QUEUED).update(status=READY)``) wins
    the race against concurrent dispatchers reading the same row. A losing race or a
    unique-constraint violation against a peer claim is silently retried on the next
    QUEUED row. Enqueue failures mark the row FAILED (with ``finished_at`` set) and
    loop to the next sibling so a single bad row does not block the session — bounded
    by ``MAX_CONSECUTIVE_DISPATCH_FAILURES`` so a broker outage doesn't mass-fail the
    whole backlog.

    ``skip_dispatch=True`` (passed by re-emits from dispatch-failure paths) suppresses
    re-entry while still letting notification receivers fire.
    """
    from sessions.models import Run, RunStatus

    if kwargs.get("skip_dispatch"):
        return

    session_id = getattr(run, "session_id", None)
    if not session_id:
        return

    consecutive_failures = 0
    while True:
        next_q = Run.objects.filter(session_id=session_id, status=RunStatus.QUEUED).order_by("created_at").first()
        if next_q is None:
            return

        try:
            claimed = Run.objects.filter(pk=next_q.pk, status=RunStatus.QUEUED).update(status=RunStatus.READY)
        except IntegrityError:
            # A concurrent insert (e.g. a fresh _submit_one) already created a
            # READY row on this session; the partial unique constraint blocks us.
            logger.debug("dispatch_next_in_session: peer claim on session=%s, backing off", session_id)
            return

        if claimed != 1:
            # Another dispatcher took this exact row; try the next one.
            continue

        next_q.refresh_from_db()
        if _enqueue_queued_run(next_q):
            return

        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_DISPATCH_FAILURES:
            logger.warning(
                "dispatch_next_in_session: bailing on session=%s after %d consecutive dispatch failures; "
                "remaining QUEUED siblings left for release_orphan_queued_sessions",
                session_id,
                consecutive_failures,
            )
            return
        # Enqueue failed; loop to the next QUEUED row.


def _enqueue_queued_run(run: Any) -> bool:
    """Enqueue ``run_job_task`` for an already-claimed (READY) Run.

    Returns ``True`` on success. On failure, marks the row FAILED with
    ``finished_at`` set and re-emits ``run_finished`` with ``skip_dispatch=True``
    so notification receivers fire without recursively re-entering the dispatcher.
    """
    from sessions.models import RunStatus

    agent_model = run.agent_model or None
    agent_thinking_level = run.agent_thinking_level or None

    try:
        task = async_to_sync(run_job_task.aenqueue)(
            repo_id=run.repo_id,
            prompt=run.prompt,
            thread_id=str(run.session_id),
            ref=run.ref or None,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            sandbox_environment_id=str(run.sandbox_environment_id) if run.sandbox_environment_id else None,
            run_id=str(run.pk),
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("dispatch_next_in_session: enqueue failed for run=%s", run.pk)
        run.save(update_fields=run.mark_failed("dispatch_failed", err))
        emit_run_finished_if_terminal(run, previous_status=RunStatus.READY, skip_dispatch=True)
        return False

    try:
        run.task_result_id = task.id
        run.save(update_fields=["task_result_id"])
    except Exception as save_err:
        # Broker holds an orphan task that won't be linked back via task_result_id.
        # Mark FAILED so siblings advance; the orphan runs but ``_sync_run_for_task``
        # no-ops because no Run row matches the task_result_id. The agent may run to
        # completion (push a commit / open an MR) while this row shows FAILED — say so.
        logger.exception("dispatch_next_in_session: failed to link task_result_id=%s to run=%s", task.id, run.pk)
        update_fields = run.mark_failed(LINK_FAILED_PREFIX, save_err)
        try:
            run.save(update_fields=update_fields)
        except Exception:
            logger.exception("dispatch_next_in_session: terminal save also failed for run=%s", run.pk)
        emit_run_finished_if_terminal(run, previous_status=RunStatus.READY, skip_dispatch=True)
        return False
    return True


def render_batch_summary(batch_id: Any, siblings: list) -> str:
    """Build the coordinator continuation prompt from denormalized Run fields."""
    from sessions.models import RunStatus

    n_ok = sum(1 for r in siblings if r.status == RunStatus.SUCCESSFUL)
    n_failed = sum(1 for r in siblings if r.status == RunStatus.FAILED)
    lines = [f"The delegated batch {batch_id} has finished ({n_ok} succeeded, {n_failed} failed).", ""]
    for r in siblings:
        state = "successful" if r.status == RunStatus.SUCCESSFUL else "failed"
        lines.append(f"## {r.repo_id} ({state})")
        if r.merge_request_web_url:
            lines.append(f"- Merge request: {r.merge_request_web_url}")
        summary = (r.result_summary or r.error_message or "").strip()
        if summary:
            lines.append(f"- Reply: {summary[:500]}")
        lines.append("")
    lines.append("Compose the consolidated outcome and continue your instructions (e.g. report back to the ticket).")
    return "\n".join(lines)


@receiver(run_finished)
def resume_coordinator_on_batch_complete(sender: type, run: Any, **kwargs: Any) -> None:
    """When every leg of a *delegated* batch is terminal, enqueue one coordinator
    continuation run on the parent thread.

    Deliberately ignores ``skip_dispatch`` (unlike ``dispatch_next_in_session``): a last
    leg that turns terminal via the dispatch-failure re-emit must still resume the
    coordinator. Winner election is the ``run_one_continuation_per_batch`` unique
    constraint; a busy coordinator session (``run_one_active_per_session``) makes the
    continuation land QUEUED, released FIFO by ``dispatch_next_in_session``.
    """
    from django.db import IntegrityError

    from asgiref.sync import async_to_sync

    from sessions.models import Run, RunStatus, Session, SessionOrigin
    from sessions.services import acreate_run

    batch_id = getattr(run, "batch_id", None)
    if not batch_id:
        return

    leg_session = Session.objects.filter(pk=run.session_id).only("parent_thread_id").first()
    if leg_session is None or not leg_session.parent_thread_id:
        return  # not a delegated leg (broadcast batch, or session gone)
    parent_thread_id = leg_session.parent_thread_id

    siblings = list(Run.objects.by_batch(batch_id))
    if any(r.status not in RunStatus.terminal() for r in siblings):
        return  # legs still pending

    if Run.objects.filter(continuation_of_batch_id=batch_id).exists():
        return  # already resumed (winner election)

    coordinator = Session.objects.filter(thread_id=parent_thread_id).first()
    if coordinator is None:
        logger.warning(
            "resume_coordinator: parent thread %s not found for batch %s; rollup notification is the fallback signal",
            parent_thread_id,
            batch_id,
        )
        return

    prompt = render_batch_summary(batch_id, siblings)
    env_id = str(coordinator.sandbox_environment_id) if coordinator.sandbox_environment_id else None
    create_kwargs = {
        "trigger_type": SessionOrigin.DELEGATED_JOB,
        "task_result_id": None,
        "repo_id": coordinator.repo_id,
        "ref": coordinator.ref,
        "user": coordinator.user,
        "prompt": prompt,
        "thread_id": parent_thread_id,
        "sandbox_environment_id": env_id,
        "continuation_of_batch_id": batch_id,
    }

    try:
        continuation = async_to_sync(acreate_run)(status=RunStatus.READY, **create_kwargs)
    except IntegrityError:
        if Run.objects.filter(continuation_of_batch_id=batch_id).exists():
            return  # another worker won the election
        # Coordinator session busy: land QUEUED; dispatch_next_in_session releases it FIFO.
        try:
            async_to_sync(acreate_run)(status=RunStatus.QUEUED, **create_kwargs)
        except IntegrityError:
            return  # another worker won in the meantime
        return

    # Free coordinator session: enqueue the READY continuation now, reusing the dispatcher helper.
    _enqueue_queued_run(continuation)
