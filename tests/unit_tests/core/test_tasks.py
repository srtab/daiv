from unittest.mock import patch

from core.tasks import prune_db_task_results_cron_task


async def test_prune_db_task_results_calls_management_command():
    with patch("core.tasks.call_command") as mock_call_command:
        await prune_db_task_results_cron_task.aenqueue()
        mock_call_command.assert_called_once_with("prune_db_task_results")
