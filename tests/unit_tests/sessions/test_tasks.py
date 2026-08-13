import uuid
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.tasks import RECLASSIFY_GRACE, reclassify_missing_envelopes_cron_task, sync_stuck_runs_cron_task


def test_sync_stuck_runs_cron_task_dispatches_command():
    """The cron task dispatches the sync_stuck_runs management command.

    Guards the wiring (command name + the ``@locked_task`` decorator that ``.func()``
    exercises), not crontask/django_tasks framework behavior.
    """
    with patch("sessions.tasks.call_command") as mock_call_command:
        sync_stuck_runs_cron_task.func()

    mock_call_command.assert_called_once_with("sync_stuck_runs")


# --- reclassify_missing_envelopes_cron_task (Epic 1 review backstop) --------


def _stranded_run(*, status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE, age=None) -> Run:
    """A scheduled Run pushed past the grace window, so the reconciler's ``created_at`` filter sees it."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(session=session, trigger_type=trigger_type, repo_id="group/repo", status=status)
    old = timezone.now() - (age if age is not None else RECLASSIFY_GRACE + timedelta(minutes=1))
    Run.objects.filter(pk=run.pk).update(created_at=old)  # created_at has a now() default
    run.refresh_from_db()
    return run


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_terminal_scheduled_runs():
    """Terminal SCHEDULE runs (SUCCESSFUL and FAILED) with no envelope are re-enqueued."""
    stranded_ok = _stranded_run(status=RunStatus.SUCCESSFUL)
    stranded_failed = _stranded_run(status=RunStatus.FAILED)

    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()

    enqueued = {call.args[0] for call in task.enqueue.call_args_list}
    assert enqueued == {str(stranded_ok.pk), str(stranded_failed.pk)}


@pytest.mark.django_db
def test_reclassify_skips_runs_that_should_not_be_reenqueued():
    """Already-classified, non-terminal, non-schedule, and too-recent runs are all left alone."""
    classified = _stranded_run()
    RunEnvelope.objects.create(run=classified, status=EnvelopeStatus.ALL_CLEAR)  # has an envelope
    _stranded_run(status=RunStatus.RUNNING)  # non-terminal
    _stranded_run(trigger_type=SessionOrigin.API_JOB)  # non-schedule
    _stranded_run(age=timedelta(minutes=1))  # within the grace window

    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()

    task.enqueue.assert_not_called()
