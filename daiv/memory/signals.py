from __future__ import annotations

import logging
from typing import Any

from django.dispatch import receiver

from activity.signals import activity_finished

from memory.tasks import extract_observations_task

logger = logging.getLogger("daiv.memory")


@receiver(activity_finished)
def capture_run_observations(sender: type, activity: Any, **kwargs: Any) -> None:
    """Enqueue transcript extraction when a run reaches a terminal status.

    FAILED runs are included — failures are valuable learning signal.
    ``skip_dispatch=True`` marks re-emits from dispatch-failure paths: those
    activities never executed, so there is no new transcript to mine.
    Exception-safe: memory capture must never affect the run lifecycle (the
    signal is robust-sent, but we don't rely on that).
    """
    from activity.models import ActivityStatus

    try:
        if kwargs.get("skip_dispatch"):
            return
        if activity.status not in ActivityStatus.terminal():
            return
        if not activity.thread_id:
            return
        extract_observations_task.enqueue(str(activity.pk))
    except Exception:
        logger.exception("capture_run_observations: failed to enqueue extraction for activity=%s", activity.pk)
