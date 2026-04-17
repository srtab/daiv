import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from activity.models import Activity, TriggerType
from django_tasks_db.models import DBTaskResult, get_date_max

from schedules.models import ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


def _make_task_result() -> MagicMock:
    """Build a real DBTaskResult row so ``create_activity`` can link to it via FK."""
    task_id = uuid.uuid4()
    DBTaskResult.objects.create(
        id=task_id,
        status="READY",
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return MagicMock(id=task_id)


@pytest.mark.django_db(transaction=True)
def test_dispatch_persists_use_max_true(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repo_id="acme/repo",
        frequency="daily",
        time="09:00",
        use_max=True,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("schedules.tasks.run_job_task") as mock_task:
        mock_task.enqueue = MagicMock(return_value=_make_task_result())
        dispatch_scheduled_jobs_cron_task.func()

    activity = Activity.objects.get(scheduled_job=schedule)
    assert activity.trigger_type == TriggerType.SCHEDULE
    assert activity.use_max is True


@pytest.mark.django_db(transaction=True)
def test_dispatch_persists_use_max_false(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repo_id="acme/repo",
        frequency="daily",
        time="09:00",
        use_max=False,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("schedules.tasks.run_job_task") as mock_task:
        mock_task.enqueue = MagicMock(return_value=_make_task_result())
        dispatch_scheduled_jobs_cron_task.func()

    activity = Activity.objects.get(scheduled_job=schedule)
    assert activity.use_max is False
