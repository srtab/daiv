from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import IntegrityError
from django.db.models.signals import post_save
from django.dispatch import Signal, receiver
from django.utils import timezone

from asgiref.sync import async_to_sync
from django_tasks.signals import task_finished, task_started
from jobs.tasks import run_job_task

logger = logging.getLogger("daiv.activity")

# Emitted when an Activity transitions to a terminal status (SUCCESSFUL or FAILED).
# Arguments: activity (Activity instance).
activity_finished = Signal()


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def backfill_activity_user(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Link orphaned activities to a newly created user by matching external_username.

    Only runs on user creation, not updates — renaming a user will not re-trigger backfill.
    Errors are caught so that a problem in activity backfill never breaks user creation.
    """
    if not created:
        return

    from activity.models import Activity

    try:
        updated = Activity.objects.filter(user__isnull=True, external_username=instance.username).update(user=instance)
    except Exception:
        logger.exception("Failed to backfill activities for new user %s (pk=%s)", instance.username, instance.pk)
        return

    if updated:
        logger.info("Backfilled %d activities for new user %s (pk=%s)", updated, instance.username, instance.pk)


def emit_activity_finished_if_terminal(
    activity: Any, previous_status: str | None, *, skip_dispatch: bool = False
) -> None:
    """Emit activity_finished if the activity just transitioned to a terminal status.

    ``skip_dispatch`` is forwarded to receivers; the in-thread dispatcher uses it to
    suppress recursive re-entry while still letting notification receivers fire.
    """
    from activity.models import ActivityStatus

    if activity.status not in ActivityStatus.terminal():
        return
    if previous_status in ActivityStatus.terminal():
        return  # Already emitted on a prior save
    results = activity_finished.send_robust(sender=type(activity), activity=activity, skip_dispatch=skip_dispatch)
    for recv, response in results:
        if isinstance(response, Exception):
            logger.error(
                "Receiver %s failed for activity_finished (activity=%s)",
                getattr(recv, "__name__", recv),
                activity.pk,
                exc_info=response,
            )


def _sync_activity_for_task(task_result_id: Any) -> None:
    """Pull latest status/timing/result from the linked DBTaskResult into the Activity row.

    Silently no-ops if no Activity is linked to the given task_result_id (e.g. tasks that
    don't create an Activity, or the brief cross-process race where ``task_started`` fires
    before the Activity row is committed on the web side — ``task_finished`` will catch up).
    Errors are swallowed and logged so the worker loop is never crashed by sync failures.
    """
    from activity.models import Activity

    try:
        activity = (
            Activity.objects
            .select_related("task_result", "scheduled_job", "scheduled_job__user", "user")
            .filter(task_result_id=task_result_id)
            .first()
        )
        if activity is None:
            return
        activity.sync_and_save()
    except Exception:
        logger.exception("Failed to sync activity for task_result_id=%s", task_result_id)


@receiver([task_started, task_finished])
def sync_activity_on_task_signal(sender: type, task_result: Any, **kwargs: Any) -> None:
    """Sync the linked Activity on task state transitions (RUNNING / terminal).

    The django-tasks worker commits DBTaskResult state (via ``claim``/``set_successful``/
    ``set_failed``) before dispatching these signals, so reading back the row here is safe
    without a transaction guard.
    """
    _sync_activity_for_task(task_result.id)


#: Cap on consecutive enqueue failures before the dispatcher bails. A persistent
#: broker outage would otherwise mass-fail every QUEUED row on the thread within
#: a single signal-handler call; bailing leaves the rest QUEUED for
#: ``release_orphan_queued_threads`` to recover when the broker is back.
MAX_CONSECUTIVE_DISPATCH_FAILURES = 3


@receiver(activity_finished)
def dispatch_next_in_thread(sender: type, activity: Any, **kwargs: Any) -> None:
    """Release queued continuations on this thread, one at a time, until one succeeds.

    Atomic compare-and-swap (``filter(pk=, status=QUEUED).update(status=READY)``) wins
    the race against concurrent dispatchers reading the same row. A losing race or a
    unique-constraint violation against a peer claim is silently retried on the next
    QUEUED row. Enqueue failures mark the row FAILED (with ``finished_at`` set) and
    loop to the next sibling so a single bad row does not block the thread — bounded
    by ``MAX_CONSECUTIVE_DISPATCH_FAILURES`` so a broker outage doesn't mass-fail the
    whole backlog.

    ``skip_dispatch=True`` (passed by re-emits from dispatch-failure paths) suppresses
    re-entry while still letting notification receivers fire.
    """
    from activity.models import Activity, ActivityStatus

    if kwargs.get("skip_dispatch"):
        return

    thread_id = getattr(activity, "thread_id", None)
    if not thread_id:
        return

    consecutive_failures = 0
    while True:
        next_q = (
            Activity.objects.filter(thread_id=thread_id, status=ActivityStatus.QUEUED).order_by("created_at").first()
        )
        if next_q is None:
            return

        try:
            claimed = Activity.objects.filter(pk=next_q.pk, status=ActivityStatus.QUEUED).update(
                status=ActivityStatus.READY
            )
        except IntegrityError:
            # A concurrent insert (e.g. a fresh _submit_one) already created a
            # READY row on this thread; the partial unique constraint blocks us.
            logger.debug("dispatch_next_in_thread: peer claim on thread=%s, backing off", thread_id)
            return

        if claimed != 1:
            # Another dispatcher took this exact row; try the next one.
            continue

        next_q.refresh_from_db()
        if _enqueue_queued_activity(next_q):
            return

        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_DISPATCH_FAILURES:
            logger.warning(
                "dispatch_next_in_thread: bailing on thread=%s after %d consecutive dispatch failures; "
                "remaining QUEUED siblings left for release_orphan_queued_threads",
                thread_id,
                consecutive_failures,
            )
            return
        # Enqueue failed; loop to the next QUEUED row.


def _enqueue_queued_activity(activity: Any) -> bool:
    """Enqueue ``run_job_task`` for an already-claimed (READY) Activity.

    Returns ``True`` on success. On failure, marks the row FAILED with
    ``finished_at`` set and re-emits ``activity_finished`` with ``skip_dispatch=True``
    so notification receivers fire without recursively re-entering the dispatcher.
    """
    from activity.models import ActivityStatus

    try:
        task = async_to_sync(run_job_task.aenqueue)(
            repo_id=activity.repo_id,
            prompt=activity.prompt,
            thread_id=activity.thread_id,
            ref=activity.ref or None,
            use_max=activity.use_max,
            sandbox_environment_id=str(activity.sandbox_environment_id) if activity.sandbox_environment_id else None,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("dispatch_next_in_thread: enqueue failed for activity=%s", activity.pk)
        now = timezone.now()
        activity.status = ActivityStatus.FAILED
        activity.error_message = f"dispatch_failed: {type(err).__name__}: {err}"
        activity.finished_at = now
        if activity.started_at is None:
            activity.started_at = now
        activity.save(update_fields=["status", "error_message", "finished_at", "started_at"])
        emit_activity_finished_if_terminal(activity, previous_status=ActivityStatus.READY, skip_dispatch=True)
        return False

    try:
        activity.task_result_id = task.id
        activity.save(update_fields=["task_result_id"])
    except Exception as save_err:
        # Broker holds an orphan task that won't be linked back via task_result_id.
        # Mark FAILED so siblings advance; the orphan runs but ``_sync_activity_for_task``
        # no-ops because no Activity row matches the task_result_id.
        logger.exception(
            "dispatch_next_in_thread: failed to link task_result_id=%s to activity=%s", task.id, activity.pk
        )
        now = timezone.now()
        activity.status = ActivityStatus.FAILED
        activity.error_message = f"link_failed: {type(save_err).__name__}: {save_err}"
        activity.finished_at = now
        if activity.started_at is None:
            activity.started_at = now
        try:
            activity.save(update_fields=["status", "error_message", "finished_at", "started_at"])
        except Exception:
            logger.exception("dispatch_next_in_thread: terminal save also failed for activity=%s", activity.pk)
        emit_activity_finished_if_terminal(activity, previous_status=ActivityStatus.READY, skip_dispatch=True)
        return False
    return True
