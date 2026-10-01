import logging
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from django.db import models, transaction
from django.db.models.functions import Coalesce

from crontask import cron
from django_tasks import task

from schedules.models import DispatchError, ScheduledJob

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("daiv.schedules")


def _record_dispatch_failure(
    schedule: ScheduledJob, now: datetime, error: DispatchError, repo_ids: Sequence[str] = ()
) -> None:
    """Record a failed dispatch and notify the owner.

    Still advance ``next_run_at`` so the schedule does not busy-retry every minute; if even that
    fails, disable the schedule (keeping the failure on record) so it stops wedging the dispatcher.
    Each write gets its own savepoint so a database error can't abort the other schedules' dispatches.
    """
    from notifications.schedule_notifiers import emit_schedule_dispatch_failed

    try:
        with transaction.atomic():
            schedule.refresh_from_db()
            failure_fields = schedule.record_dispatch_failure(error, at=now, repo_ids=repo_ids)
            advance_fields = schedule.advance_after_dispatch(after=now)
            schedule.save(update_fields=["modified", *failure_fields, *advance_fields])
    except Exception:
        logger.exception(
            "Failed to advance next_run_at for scheduled job pk=%d (%s); disabling schedule", schedule.pk, schedule.name
        )
        try:
            with transaction.atomic():
                ScheduledJob.objects.filter(pk=schedule.pk).update(
                    is_enabled=False,
                    next_run_at=None,
                    failing_since=Coalesce("failing_since", models.Value(now)),
                    dispatch_error=error,
                    dispatch_error_repo_ids=list(repo_ids),
                    modified=now,
                )
        except Exception:
            logger.exception("Failed to disable stuck scheduled job pk=%d (%s)", schedule.pk, schedule.name)
            return

    transaction.on_commit(partial(emit_schedule_dispatch_failed, schedule.pk), robust=True)


@cron("* * * * *")
@task
def dispatch_scheduled_jobs_cron_task():
    """Check for scheduled jobs that are due and enqueue them.

    Uses ``select_for_update(skip_locked=True)`` so that if the dispatcher
    overlaps (takes >1 minute), the same schedule is not double-dispatched.
    Each schedule is processed in its own savepoint so that one failure
    does not roll back updates for other schedules.
    """
    from sandbox_envs.selection import resolve_repo_envs
    from sessions.models import SessionOrigin
    from sessions.services import RepoTarget, submit_batch_runs

    from accounts.models import User
    from codebase.authorization import RepositoryAccessDenied

    now = datetime.now(tz=UTC)
    dispatched = 0
    failed = 0

    with transaction.atomic():
        due_schedules = list(
            ScheduledJob.objects.select_for_update(skip_locked=True).filter(
                is_enabled=True,
                next_run_at__lte=now,
                # Skip schedules owned by inactivated users. Expressed as a subquery rather
                # than a ``user__is_active`` join so FOR UPDATE SKIP LOCKED locks only the
                # ScheduledJob table. A join would pull the user table into the lock set, so a
                # lock held on an owner's user row would make SKIP LOCKED drop that owner's due
                # schedules even though the schedule rows themselves are free.
                user_id__in=User.objects.filter(is_active=True).values("pk"),
            )
        )

        for schedule in due_schedules:
            error: DispatchError | None = None
            repo_ids: Sequence[str] = ()
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
                        trigger_type=SessionOrigin.SCHEDULE,
                        scheduled_job=schedule,
                        mcp_overrides=schedule.mcp_overrides,
                    )
                    if result.runs:
                        schedule.last_run_at = now
                        schedule.last_run_batch_id = result.batch_id
                        schedule.run_count = models.F("run_count") + 1
                        advance_fields = schedule.advance_after_dispatch(after=now)
                        recovery_fields = schedule.clear_dispatch_failure()
                        schedule.save(
                            update_fields=[
                                "last_run_at",
                                "last_run_batch_id",
                                "run_count",
                                "modified",
                                *advance_fields,
                                *recovery_fields,
                            ]
                        )
            except RepositoryAccessDenied as err:
                logger.warning(
                    "Scheduled job pk=%d (%s) skipped: owner lacks access to %s",
                    schedule.pk,
                    schedule.name,
                    err.repo_ids,
                )
                error, repo_ids = DispatchError.REPO_ACCESS_DENIED, err.repo_ids
            except Exception:
                logger.exception("Failed to dispatch scheduled job pk=%d (%s)", schedule.pk, schedule.name)
                error = DispatchError.UNEXPECTED
            else:
                if not result.runs:
                    logger.warning(
                        "Scheduled job pk=%d (%s) started no runs: every repository failed to enqueue: %s",
                        schedule.pk,
                        schedule.name,
                        [f.repo_id for f in result.failed],
                    )
                    error = DispatchError.UNEXPECTED
                elif result.failed:
                    logger.warning(
                        "Scheduled job pk=%d dispatched with %d per-repo enqueue failures: %s",
                        schedule.pk,
                        len(result.failed),
                        [f.repo_id for f in result.failed],
                    )

            if error is None:
                dispatched += 1
            else:
                failed += 1
                _record_dispatch_failure(schedule, now, error, repo_ids)

    if dispatched:
        logger.info("Dispatched %d scheduled job(s)", dispatched)
    if failed:
        logger.warning("%d scheduled job(s) failed to dispatch", failed)
