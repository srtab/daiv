from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from django_tasks.signals import task_finished, task_started

logger = logging.getLogger("daiv.activity")


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


def _sync_activity_for_task(task_result_id: Any) -> None:
    """Pull latest status/timing/result from the linked DBTaskResult into the Activity row.

    Silently no-ops if no Activity is linked to the given task_result_id (e.g. tasks that
    don't create an Activity, or the brief cross-process race where ``task_started`` fires
    before the Activity row is committed on the web side — ``task_finished`` will catch up).
    Errors are swallowed and logged so the worker loop is never crashed by sync failures.
    """
    from activity.models import Activity

    try:
        activity = Activity.objects.select_related("task_result").filter(task_result_id=task_result_id).first()
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
