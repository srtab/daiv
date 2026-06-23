from unittest.mock import patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished
from memory.signals import capture_run_observations


def _activity(**kwargs):
    defaults = {
        "trigger_type": TriggerType.API_JOB,
        "repo_id": "group/project",
        "status": ActivityStatus.SUCCESSFUL,
        "thread_id": "thread-1",
    }
    defaults.update(kwargs)
    return Activity.objects.create(**defaults)


@pytest.mark.django_db
class TestCaptureRunObservations:
    def test_enqueues_for_successful_with_thread_id(self):
        activity = _activity()
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Activity, activity=activity)
        task_mock.enqueue.assert_called_once_with(str(activity.pk))

    def test_enqueues_for_failed_runs_too(self):
        """Failures are valuable learning signal."""
        activity = _activity(status=ActivityStatus.FAILED)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Activity, activity=activity)
        task_mock.enqueue.assert_called_once_with(str(activity.pk))

    def test_skips_non_terminal_status(self):
        activity = _activity(status=ActivityStatus.RUNNING)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Activity, activity=activity)
        task_mock.enqueue.assert_not_called()

    def test_skips_missing_thread_id(self):
        activity = _activity(thread_id=None)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Activity, activity=activity)
        task_mock.enqueue.assert_not_called()

    def test_skips_dispatch_failure_reemits(self):
        """skip_dispatch=True marks re-emits for activities that never actually ran."""
        activity = _activity(status=ActivityStatus.FAILED)
        with patch("memory.signals.extract_observations_task") as task_mock:
            capture_run_observations(sender=Activity, activity=activity, skip_dispatch=True)
        task_mock.enqueue.assert_not_called()

    def test_never_raises_on_enqueue_failure(self):
        activity = _activity()
        with patch("memory.signals.extract_observations_task") as task_mock:
            task_mock.enqueue.side_effect = RuntimeError("broker down")
            capture_run_observations(sender=Activity, activity=activity)  # must not raise

    def test_wired_to_activity_finished_signal(self):
        """apps.ready() must register the receiver on the real signal."""
        activity = _activity()
        with patch("memory.signals.extract_observations_task") as task_mock:
            activity_finished.send(sender=Activity, activity=activity)
        task_mock.enqueue.assert_called_once_with(str(activity.pk))
