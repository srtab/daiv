import uuid
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.tasks import (
    RECLASSIFY_GRACE,
    RECLASSIFY_MAX_AGE,
    reclassify_missing_envelopes_cron_task,
    sync_stuck_runs_cron_task,
)


def test_sync_stuck_runs_cron_task_dispatches_command():
    """The cron task dispatches the sync_stuck_runs management command.

    Guards the wiring (command name + the ``@locked_task`` decorator that ``.func()``
    exercises), not crontask/django_tasks framework behavior.
    """
    with patch("sessions.tasks.call_command") as mock_call_command:
        sync_stuck_runs_cron_task.func()

    mock_call_command.assert_called_once_with("sync_stuck_runs")


# --- reclassify_missing_envelopes_cron_task (Epic 1 review backstop) --------


def _stranded_run(*, status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE, finished_age=None) -> Run:
    """A terminal Run whose finished_at is pushed past the grace window (and inside the max-age floor)."""
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(session=session, trigger_type=trigger_type, repo_id="group/repo", status=status)
    finished = timezone.now() - (finished_age if finished_age is not None else RECLASSIFY_GRACE + timedelta(minutes=1))
    Run.objects.filter(pk=run.pk).update(finished_at=finished)
    run.refresh_from_db()
    return run


@pytest.mark.django_db
def test_reclassify_reenqueues_stranded_terminal_runs_across_origins():
    stranded = [
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.SCHEDULE),
        _stranded_run(status=RunStatus.FAILED, trigger_type=SessionOrigin.MR_WEBHOOK),
        _stranded_run(status=RunStatus.SUCCESSFUL, trigger_type=SessionOrigin.API_JOB),
    ]
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    enqueued = {call.args[0] for call in task.enqueue.call_args_list}
    assert enqueued == {str(r.pk) for r in stranded}


@pytest.mark.django_db
def test_reclassify_skips_chat_classified_nonterminal_and_out_of_window():
    classified = _stranded_run()
    RunEnvelope.objects.create(run=classified, status=EnvelopeStatus.ALL_CLEAR)  # already has an envelope
    _stranded_run(status=RunStatus.RUNNING)  # non-terminal
    _stranded_run(trigger_type=SessionOrigin.CHAT)  # chat is never classified
    _stranded_run(finished_age=timedelta(minutes=1))  # inside the grace window
    _stranded_run(finished_age=RECLASSIFY_MAX_AGE + timedelta(hours=1))  # older than the recency floor
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    task.enqueue.assert_not_called()


@pytest.mark.django_db
def test_reclassify_keys_on_finished_at_not_created_at():
    # created_at old but finished_at recent → still re-targeted (batch siblings can finish long after creation).
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.SCHEDULE, repo_id="group/repo")
    run = Run.objects.create(
        session=session, trigger_type=SessionOrigin.SCHEDULE, repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )
    Run.objects.filter(pk=run.pk).update(
        created_at=timezone.now() - (RECLASSIFY_MAX_AGE + timedelta(days=2)),
        finished_at=timezone.now() - (RECLASSIFY_GRACE + timedelta(minutes=1)),
    )
    with patch("sessions.tasks.classify_run_task") as task:
        reclassify_missing_envelopes_cron_task.func()
    assert {call.args[0] for call in task.enqueue.call_args_list} == {str(run.pk)}
