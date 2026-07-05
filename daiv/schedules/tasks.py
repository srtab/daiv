import logging
from datetime import UTC, datetime

from django.db import models, transaction

from crontask import cron
from django_tasks import task

from schedules.models import ScheduledJob

logger = logging.getLogger("daiv.schedules")


def _advance_or_disable(schedule: ScheduledJob, now: datetime) -> None:
    """Recover a schedule after a failed dispatch.

    Still advance ``next_run_at`` so the schedule does not busy-retry every minute; if even that
    fails, disable the schedule so it stops wedging the dispatcher. Shared by every dispatch
    failure path (access-denied and unexpected errors alike).
    """
    try:
        schedule.refresh_from_db(fields=["run_count", "last_run_at", "last_run_batch_id", "next_run_at"])
        advance_fields = schedule.advance_after_dispatch(after=now)
        schedule.save(update_fields=["modified", *advance_fields])
    except Exception:
        logger.exception(
            "Failed to advance next_run_at for scheduled job pk=%d (%s); disabling schedule", schedule.pk, schedule.name
        )
        try:
            ScheduledJob.objects.filter(pk=schedule.pk).update(is_enabled=False)
        except Exception:
            logger.exception("Failed to disable stuck scheduled job pk=%d (%s)", schedule.pk, schedule.name)


@cron("* * * * *")
@task
def dispatch_scheduled_jobs_cron_task():
    """Check for scheduled jobs that are due and enqueue them.

    Uses ``select_for_update(skip_locked=True)`` so that if the dispatcher
    overlaps (takes >1 minute), the same schedule is not double-dispatched.
    Each schedule is processed in its own savepoint so that one failure
    does not roll back updates for other schedules.
    """
    from activity.models import TriggerType
    from activity.services import RepoTarget, submit_batch_runs
    from sandbox_envs.services import resolve_repo_envs

    from codebase.authorization import RepositoryAccessDenied

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
                    repos = [RepoTarget(repo_id=r["repo_id"], ref=r["ref"]) for r in schedule.repos]
                    repos = resolve_repo_envs(
                        user=schedule.user,
                        repos=repos,
                        explicit_env_id=(
                            str(schedule.sandbox_environment_id) if schedule.sandbox_environment_id else None
                        ),
                    )
                    result = submit_batch_runs(
                        user=schedule.user,
                        prompt=schedule.prompt,
                        repos=repos,
                        agent_model=schedule.agent_model,
                        agent_thinking_level=schedule.agent_thinking_level,
                        notify_on=None,
                        trigger_type=TriggerType.SCHEDULE,
                        scheduled_job=schedule,
                    )
                    schedule.last_run_at = now
                    schedule.last_run_batch_id = result.batch_id
                    schedule.run_count = models.F("run_count") + 1
                    advance_fields = schedule.advance_after_dispatch(after=now)
                    schedule.save(
                        update_fields=["last_run_at", "last_run_batch_id", "run_count", "modified", *advance_fields]
                    )
                dispatched += 1
                if result.failed:
                    logger.warning(
                        "Scheduled job pk=%d dispatched with %d per-repo enqueue failures: %s",
                        schedule.pk,
                        len(result.failed),
                        [f.repo_id for f in result.failed],
                    )
            except RepositoryAccessDenied:
                logger.warning(
                    "Scheduled job pk=%d (%s) skipped: owner lacks access to its repositories",
                    schedule.pk,
                    schedule.name,
                )
                failed += 1
                _advance_or_disable(schedule, now)
            except Exception:
                logger.exception("Failed to dispatch scheduled job pk=%d (%s)", schedule.pk, schedule.name)
                failed += 1
                _advance_or_disable(schedule, now)

    if dispatched:
        logger.info("Dispatched %d scheduled job(s)", dispatched)
    if failed:
        logger.warning("%d scheduled job(s) failed to dispatch", failed)
