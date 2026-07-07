from __future__ import annotations

import logging
from typing import Any

from django.dispatch import receiver

from sessions.signals import run_finished

from memory.tasks import extract_observations_task

logger = logging.getLogger("daiv.memory")


@receiver(run_finished)
def capture_run_observations(sender: type, run: Any, **kwargs: Any) -> None:
    """Enqueue transcript extraction when a run reaches a terminal status.

    FAILED runs are included — failures are valuable learning signal.
    ``skip_dispatch=True`` marks re-emits from dispatch-failure paths: those
    runs never executed, so there is no new transcript to mine.
    CHAT-triggered runs are skipped — interactive chat turns are not agent
    sessions worth mining for repository-scoped memory.
    Exception-safe: memory capture must never affect the run lifecycle (the
    signal is robust-sent, but we don't rely on that).
    """
    from sessions.models import RunStatus, SessionOrigin

    try:
        if kwargs.get("skip_dispatch"):
            return
        if run.trigger_type == SessionOrigin.CHAT:
            return
        if run.status not in RunStatus.terminal():
            return
        if not run.session_id:
            return
        extract_observations_task.enqueue(str(run.pk))
    except Exception:
        logger.exception("capture_run_observations: failed to enqueue extraction for run=%s", run.pk)
