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


async def _amake_task_result() -> MagicMock:
    task_id = uuid.uuid4()
    await DBTaskResult.objects.acreate(
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
def test_dispatch_single_repo_persists_use_max_true(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        use_max=True,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("activity.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    activity = Activity.objects.get(scheduled_job=schedule)
    assert activity.trigger_type == TriggerType.SCHEDULE
    assert activity.use_max is True
    assert activity.batch_id is not None


@pytest.mark.django_db(transaction=True)
def test_dispatch_single_repo_persists_use_max_false(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        use_max=False,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("activity.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    activity = Activity.objects.get(scheduled_job=schedule)
    assert activity.use_max is False


@pytest.mark.django_db(transaction=True)
def test_dispatch_three_repos_creates_three_activities_sharing_batch(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "o/a", "ref": ""}, {"repo_id": "o/b", "ref": "dev"}, {"repo_id": "o/c", "ref": ""}],
        frequency="daily",
        time="09:00",
        use_max=False,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("activity.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    activities = list(Activity.objects.filter(scheduled_job=schedule))
    assert len(activities) == 3
    batches = {a.batch_id for a in activities}
    assert len(batches) == 1
    schedule.refresh_from_db()
    assert schedule.last_run_batch_id == next(iter(batches))


@pytest.mark.django_db(transaction=True)
def test_dispatch_advances_next_run_on_success(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="x",
        repos=[{"repo_id": "x/y", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("activity.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert schedule.next_run_at is not None
    assert schedule.next_run_at > past
