import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from schedules.models import Frequency, ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


@pytest.mark.django_db(transaction=True)
def test_dispatch_stamps_explicit_env_on_targets(member_user):
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
    fake_result.runs = []
    fake_result.failed = []

    with patch("sessions.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    assert submit.call_count == 1
    targets = submit.call_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [str(env.id)]
    assert submit.call_args.kwargs["scheduled_job"].pk == schedule.pk


@pytest.mark.django_db(transaction=True)
def test_dispatch_auto_resolves_to_global_default(member_user):
    """Schedule with no explicit env → dispatch resolves Auto to the GLOBAL default."""
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    default = SandboxEnvironment.objects.create(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
    )
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
    fake_result.runs = []
    fake_result.failed = []

    with patch("sessions.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    targets = submit.call_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [str(default.id)]


@pytest.mark.django_db(transaction=True)
def test_dispatch_auto_resolves_user_env_for_schedule_owner(member_user):
    """Schedule with no explicit env + USER env owned by schedule.user claiming the repo →
    dispatch resolves to the USER env."""
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
    user_env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=member_user, name="mine", base_image="python:3.14", repo_ids=["acme/foo"]
    )
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "acme/foo", "ref": ""}],
        frequency=Frequency.DAILY,
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    fake_result = MagicMock()
    fake_result.batch_id = uuid.uuid4()
    fake_result.runs = []
    fake_result.failed = []

    with patch("sessions.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    targets = submit.call_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [str(user_env.id)]


@pytest.mark.django_db(transaction=True)
def test_dispatch_auto_with_no_envs_stays_none(member_user):
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
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
    fake_result.runs = []
    fake_result.failed = []

    with patch("sessions.services.submit_batch_runs", return_value=fake_result) as submit:
        dispatch_scheduled_jobs_cron_task.func()

    targets = submit.call_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [None]
