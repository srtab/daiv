from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from activity.models import TriggerType

from schedules.models import ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


@pytest.mark.django_db(transaction=True)
def test_dispatch_forwards_use_max_true_to_create_activity(member_user):
    """When a scheduled job has ``use_max=True``, the dispatched Activity inherits it."""
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    ScheduledJob.objects.create(
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

    fake_result = MagicMock()
    fake_result.id = "00000000-0000-0000-0000-000000000001"

    with patch("schedules.tasks.run_job_task") as mock_task, patch("activity.services.create_activity") as mock_create:
        mock_task.enqueue = MagicMock(return_value=fake_result)
        dispatch_scheduled_jobs_cron_task.func()

    assert mock_create.call_args.kwargs["use_max"] is True
    assert mock_create.call_args.kwargs["trigger_type"] == TriggerType.SCHEDULE


@pytest.mark.django_db(transaction=True)
def test_dispatch_forwards_use_max_false(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    ScheduledJob.objects.create(
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

    fake_result = MagicMock()
    fake_result.id = "00000000-0000-0000-0000-000000000002"

    with patch("schedules.tasks.run_job_task") as mock_task, patch("activity.services.create_activity") as mock_create:
        mock_task.enqueue = MagicMock(return_value=fake_result)
        dispatch_scheduled_jobs_cron_task.func()

    assert mock_create.call_args.kwargs["use_max"] is False
