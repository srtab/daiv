from __future__ import annotations

import logging
from functools import partial
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.tasks.signals import task_finished, task_started

from django_tasks_db.backend import TaskResult as DatabaseTaskResult

from core import ui_events
from core.constants import CrossProjectOutcome
from sessions.models import Run, RunStatus, Session
from sessions.queue import dispatch_next_queued
from sessions.signal_defs import run_finished

logger = logging.getLogger("daiv.sessions")


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def backfill_session_user(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Link orphaned runs and sessions to a newly created user by matching external_username.

    Only runs on user creation, not updates — renaming a user will not re-trigger backfill.
    Errors are caught so that a problem in session backfill never breaks user creation.
    """
    if not created:
        return

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


@receiver(post_save, sender=Run, dispatch_uid="sessions.publish_nav_runs_changed")
def publish_nav_runs_changed(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Poke the nav SSE readers whenever a Run's status may have moved.

    The sidebar's "N running" badge is per-viewer, so readers recompute their own
    count from this poke (see ``core.ui_events``). Deferred to commit so the recount
    cannot read a pre-write snapshot.

    On an update the gate is a deliberate superset — a full save carries no
    ``update_fields`` to inspect — because an extra poke costs one COUNT per reader
    while a missed one leaves a stale badge until the connection's next resync. A
    create is exact instead: the status is in hand, and every other run creation
    (``READY``/``QUEUED`` from the webhook, API, MCP, schedule and batch paths) is a
    row no reader's count can include. A run's finish and a sync's non-terminal write skip this
    signal: ``RunManager.finish`` and ``RunManager.update_unfinished`` refresh the badge themselves.
    """
    if created:
        if instance.status == RunStatus.RUNNING:
            transaction.on_commit(ui_events.publisher.runs_changed)
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        return
    transaction.on_commit(ui_events.publisher.runs_changed)


@receiver(
    post_save, sender="codebase.CrossProjectAccessRecord", dispatch_uid="sessions.restrict_session_to_cross_project"
)
def restrict_session_to_cross_project_fetchers(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
    """Add the person whose grant fetched another project's content to the session's ``cross_project_user_ids``.

    Errors propagate on purpose, so whoever writes the record learns the session could not be restricted. A
    thread with no session row raises too: a session created over it later would start unrestricted. So does a
    record with no thread at all, since nothing could be restricted. One-shot runs, the only sessionless threads,
    get no cross-project access (``sessions.executor.run._agent_run``).
    """
    if not created or instance.outcome != CrossProjectOutcome.ALLOWED:
        return
    if not instance.thread_id:
        logger.error("Cross-project fetch pk=%s names no thread, so no session can be restricted.", instance.pk)
        raise ValueError("An allowed cross-project access record must name its thread.")
    user_id = instance.acting_user_id
    if user_id is None:
        logger.error(
            "Cross-project fetch in thread_id=%s names no acting user; only admins will see the session.",
            instance.thread_id,
        )
    if Session.objects.filter(pk=instance.thread_id, cross_project_user_ids__contains=[user_id]).exists():
        return
    with transaction.atomic():
        session = (
            Session.objects.select_for_update().only("cross_project_user_ids").filter(pk=instance.thread_id).first()
        )
        if session is None:
            logger.error("Cross-project fetch in thread_id=%s has no session row to restrict.", instance.thread_id)
            raise Session.DoesNotExist(f"No session row for thread_id={instance.thread_id}")
        if user_id in session.cross_project_user_ids:
            return
        session.cross_project_user_ids = [*session.cross_project_user_ids, user_id]
        session.save(update_fields=["cross_project_user_ids"])


def _sync_run_for_task(task_result_id: Any) -> None:
    """Pull latest status/timing/result from the linked DBTaskResult into the Run row.

    Silently no-ops if no Run is linked to the given task_result_id (e.g. tasks that
    don't create a Run, or the brief cross-process race where ``task_started`` fires
    before the Run row is committed on the web side — ``task_finished`` will catch up).
    Errors are swallowed and logged so the worker loop is never crashed by sync failures.
    """
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
    without a transaction guard. Only a database result can link to a Run, so results from
    any other backend (the test ``ImmediateBackend``) are skipped.
    """
    if not isinstance(task_result, DatabaseTaskResult):
        return
    _sync_run_for_task(task_result.id)


@receiver(run_finished)
def dispatch_next_in_session(sender: type, run: Any, **kwargs: Any) -> None:
    """Release the next QUEUED run on the finished run's session (:func:`sessions.queue.dispatch_next_queued`).

    ``skip_dispatch=True`` (set when the dispatcher or orphan sweep fails a run it could not enqueue or link)
    suppresses re-entry while still letting the other receivers fire.
    """
    if kwargs.get("skip_dispatch"):
        return
    if session_id := getattr(run, "session_id", None):
        dispatch_next_queued(session_id)


@receiver(post_delete, sender="agent_sessions.RunArtifact", dispatch_uid="sessions.delete_artifact_file")
def delete_artifact_file(sender: type, instance: Any, **kwargs: Any) -> None:
    """Remove the stored bytes once the row's deletion commits (Django never deletes FileField content itself)."""
    from sessions.artifacts import delete_artifact_files

    if instance.file.name:
        transaction.on_commit(
            partial(delete_artifact_files, instance.file.storage, instance.file.name, str(instance.pk))
        )
