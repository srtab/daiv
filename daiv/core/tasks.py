from django.core.management import call_command
from django.tasks import task

from crontask import cron


@cron("0 0 * * *")  # every day at midnight
@task
def prune_db_task_results_cron_task():
    """
    Prune database task results every day at midnight.
    """
    call_command("prune_db_task_results")  # noqa: S106


@cron("0 1 * * *")  # every day at 1 AM
@task
def cleanup_checkpoints_cron_task():
    """
    Delete checkpoint data for agent threads older than 180 days.
    """
    call_command("cleanup_checkpoints", "--days", "180")  # noqa: S106
