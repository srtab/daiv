from django.core.management import call_command
from django.tasks import task

from crontask import cron


@cron("0 0 * * *")  # every day at midnight
@task
async def prune_db_task_results_cron_task():
    """
    Prune database task results every day at midnight.
    """
    call_command("prune_db_task_results")  # noqa: S106
