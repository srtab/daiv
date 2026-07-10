from unittest.mock import patch

from sessions.tasks import sync_stuck_runs_cron_task


def test_sync_stuck_runs_cron_task_dispatches_command():
    """The cron task dispatches the sync_stuck_runs management command.

    Guards the wiring (command name + the ``@locked_task`` decorator that ``.func()``
    exercises), not crontask/django_tasks framework behavior.
    """
    with patch("sessions.tasks.call_command") as mock_call_command:
        sync_stuck_runs_cron_task.func()

    mock_call_command.assert_called_once_with("sync_stuck_runs")
