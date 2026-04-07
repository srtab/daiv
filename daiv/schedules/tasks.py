import logging
from datetime import UTC, datetime

from django.db import models, transaction

from crontask import cron
from django_tasks import task
from jobs.tasks import run_job_task

logger = logging.getLogger("daiv.schedules")


@cron("* * * * *")
@task
def dispatch_scheduled_jobs_cron_task():
    """Check for scheduled jobs that are due and enqueue them.

    Uses ``select_for_update(skip_locked=True)`` so that if the dispatcher
    overlaps (takes >1 minute), the same schedule is not double-dispatched.
    Each schedule is processed in its own savepoint so that one failure
    does not roll back updates for other schedules.
    """
    from schedules.models import ScheduledJob, ScheduledJobRun

    now = datetime.now(tz=UTC)
    dispatched = 0
    failed = 0

    with transaction.atomic():
        due_schedules = list(
            ScheduledJob.objects.select_for_update(skip_locked=True).filter(is_enabled=True, next_run_at__lte=now)
        )

        for schedule in due_schedules:
            try:
                with transaction.atomic():
                    ref = schedule.ref or None
                    result = run_job_task.enqueue(repo_id=schedule.repo_id, prompt=schedule.prompt, ref=ref)
                    ScheduledJobRun.objects.create(scheduled_job=schedule, task_result_id=result.id)
                    schedule.last_run_at = now
                    schedule.last_run_task_id = result.id
                    schedule.run_count = models.F("run_count") + 1
                    schedule.compute_next_run(after=now)
                    schedule.save(
                        update_fields=["last_run_at", "last_run_task_id", "run_count", "next_run_at", "modified"]
                    )
                    dispatched += 1
            except Exception:
                logger.exception("Failed to dispatch scheduled job pk=%d (%s)", schedule.pk, schedule.name)
                failed += 1
                try:
                    schedule.refresh_from_db(fields=["run_count", "last_run_at", "last_run_task_id", "next_run_at"])
                    schedule.compute_next_run(after=now)
                    schedule.save(update_fields=["next_run_at", "modified"])
                except Exception:
                    logger.exception(
                        "Failed to advance next_run_at for scheduled job pk=%d (%s); disabling schedule",
                        schedule.pk,
                        schedule.name,
                    )
                    try:
                        ScheduledJob.objects.filter(pk=schedule.pk).update(is_enabled=False)
                    except Exception:
                        logger.exception("Failed to disable stuck scheduled job pk=%d (%s)", schedule.pk, schedule.name)

    if dispatched:
        logger.info("Dispatched %d scheduled job(s)", dispatched)
    if failed:
        logger.warning("%d scheduled job(s) failed to dispatch", failed)
