from unittest.mock import patch

import pytest
from memory.signals import capture_run_observations
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signals import run_finished


def _session(**kwargs):
    defaults = {"thread_id": "thread-1", "origin": SessionOrigin.API_JOB, "repo_id": "group/project"}
    defaults.update(kwargs)
    return Session.objects.create(**defaults)


def _run(session, **kwargs):
    defaults = {"trigger_type": SessionOrigin.API_JOB, "repo_id": "group/project", "status": RunStatus.SUCCESSFUL}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


@pytest.mark.django_db
class TestCaptureRunObservations:
    def test_enqueues_for_successful_with_thread_id(self):
        session = _session()
        run = _run(session)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run)
        task_mock.enqueue.assert_called_once_with(str(run.pk))

    def test_enqueues_for_failed_runs_too(self):
        """Failures are valuable learning signal."""
        session = _session()
        run = _run(session, status=RunStatus.FAILED)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run)
        task_mock.enqueue.assert_called_once_with(str(run.pk))

    def test_skips_non_terminal_status(self):
        session = _session()
        run = _run(session, status=RunStatus.RUNNING)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run)
        task_mock.enqueue.assert_not_called()

    def test_skips_missing_session_id(self):
        """A run with no session_id (guard against stale/bad data)."""
        session = _session(thread_id="thread-no-session")
        run = _run(session)
        # Simulate missing session_id by detaching
        run.session_id = None
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run)
        task_mock.enqueue.assert_not_called()

    def test_skips_dispatch_failure_reemits(self):
        """skip_dispatch=True marks re-emits for runs that never actually executed."""
        session = _session()
        run = _run(session, status=RunStatus.FAILED)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run, skip_dispatch=True)
        task_mock.enqueue.assert_not_called()

    def test_skips_chat_trigger_runs(self):
        """Chat-triggered runs produce no memory observations."""
        session = _session(origin=SessionOrigin.CHAT, thread_id="thread-chat")
        run = _run(session, trigger_type=SessionOrigin.CHAT)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Run, run=run)
        task_mock.enqueue.assert_not_called()

    def test_never_raises_on_enqueue_failure(self):
        session = _session()
        run = _run(session)
        with patch("memory.signals.extract_observations_task") as task_mock:
            task_mock.enqueue.side_effect = RuntimeError("broker down")
            capture_run_observations(sender=Run, run=run)  # must not raise

    def test_wired_to_run_finished_signal(self):
        """apps.ready() must register the receiver on the run_finished signal."""
        session = _session()
        run = _run(session)
        with patch("memory.signals.extract_observations_task") as task_mock:
            run_finished.send(sender=Run, run=run)
        task_mock.enqueue.assert_called_once_with(str(run.pk))
