import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from schedules.models import Frequency, ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


@pytest.mark.django_db(transaction=True)
def test_dispatch_passes_sandbox_env_id_from_job(member_user):
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="dev", base_image="alpine:latest")
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "r/p", "ref": ""}],
        frequency=Frequency.DAILY,
        time="09:00",
        is_enabled=True,
        next_run_at=past,
        sandbox_environment=env,
    )

    fake_result = MagicMock()
    fake_result.batch_id = uuid.uuid4()
    fake_result.activities = []
    fake_result.failed = []

    with patch("activity.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    assert submit.call_count == 1
    assert submit.call_args.kwargs["sandbox_environment_id"] == str(env.id)
    assert submit.call_args.kwargs["scheduled_job"].pk == schedule.pk


@pytest.mark.django_db(transaction=True)
def test_dispatch_passes_none_when_job_has_no_sandbox_env(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "r/p", "ref": ""}],
        frequency=Frequency.DAILY,
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    fake_result = MagicMock()
    fake_result.batch_id = uuid.uuid4()
    fake_result.activities = []
    fake_result.failed = []

    with patch("activity.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    assert submit.call_count == 1
    assert submit.call_args.kwargs["sandbox_environment_id"] is None
