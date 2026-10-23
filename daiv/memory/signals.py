from __future__ import annotations

import logging
from typing import Any

from django.dispatch import receiver

from sessions.signal_defs import run_finished

from memory.tasks import extract_observations_task

logger = logging.getLogger("daiv.memory")


@receiver(run_finished)
def capture_run_observations(sender: type, run: Any, **kwargs: Any) -> None:
    """Enqueue transcript extraction when a run reaches a terminal status.

    FAILED runs are included — failures are valuable learning signal.
    ``never_started=True`` marks a run whose row failed before it started: the
    session's checkpoint holds only earlier turns, which their own runs already mined.
    CHAT-triggered runs are skipped — interactive chat turns are not agent
    sessions worth mining for repository-scoped memory. So are runs whose session
    holds another project's results, which must not reach this repository's memory.
    Exception-safe: memory capture must never affect the run lifecycle (the
    signal is robust-sent, but we don't rely on that).
    """
    from sessions.models import RunStatus, Session, SessionOrigin

    try:
        if kwargs.get("never_started"):
            return
        if run.trigger_type == SessionOrigin.CHAT:
            return
        if run.status not in RunStatus.terminal():
            return
        if not run.session_id:
            return
        if Session.objects.filter(pk=run.session_id).exclude(cross_project_user_ids=[]).exists():
            return
        extract_observations_task.enqueue(str(run.pk))
    except Exception:
        logger.exception("capture_run_observations: failed to enqueue extraction for run=%s", run.pk)
