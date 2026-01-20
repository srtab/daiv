from unittest.mock import patch

import pytest

import codebase.tasks as codebase_tasks


async def test_setup_webhooks_cron_task_calls_command():
    if not hasattr(codebase_tasks, "setup_webhooks_cron_task"):
        pytest.skip("setup_webhooks_cron_task is only defined for the GitLab client")

    with patch("codebase.tasks.call_command") as mock_call_command, patch("codebase.tasks.settings.DEBUG", True):
        await codebase_tasks.setup_webhooks_cron_task.aenqueue()

    mock_call_command.assert_called_once_with("setup_webhooks", disable_ssl_verification=True)
