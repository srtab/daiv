import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.models import Run, SessionOrigin

from accounts.models import User
from schedules.models import Frequency, ScheduledJob
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
def test_dispatch_single_repo_propagates_agent_override(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        agent_model="openrouter:anthropic/claude-opus-4.6",
        agent_thinking_level="high",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    run = Run.objects.get(session__scheduled_job=schedule)
    assert run.trigger_type == SessionOrigin.SCHEDULE
    assert run.agent_model == "openrouter:anthropic/claude-opus-4.6"
    assert run.agent_thinking_level == "high"
    assert run.batch_id is not None


@pytest.mark.django_db(transaction=True)
def test_dispatch_single_repo_auto_override(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    run = Run.objects.get(session__scheduled_job=schedule)
    assert run.agent_model == ""
    assert run.agent_thinking_level == ""


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
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    runs = list(Run.objects.filter(session__scheduled_job=schedule))
    assert len(runs) == 3
    batches = {r.batch_id for r in runs}
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

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert schedule.next_run_at is not None
    assert schedule.next_run_at > past


@pytest.mark.django_db(transaction=True)
def test_dispatch_once_schedule_auto_disables_on_success(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="one-off",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency=Frequency.ONCE,
        run_at=past,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert schedule.is_enabled is False
    assert schedule.next_run_at is None
    assert schedule.run_count == 1
    assert schedule.run_at == past  # preserved for audit


@pytest.mark.django_db(transaction=True)
def test_dispatch_once_schedule_auto_disables_on_failure(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="one-off",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency=Frequency.ONCE,
        run_at=past,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.submit_batch_runs", side_effect=RuntimeError("boom")):
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert schedule.is_enabled is False
    assert schedule.next_run_at is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("owner_active,expected_runs", [(True, 1), (False, 0)])
def test_dispatch_respects_owner_is_active(member_user, owner_active, expected_runs):
    member_user.is_active = owner_active
    member_user.save(update_fields=["is_active"])
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert Run.objects.count() == expected_runs
    if owner_active:
        assert schedule.next_run_at > past  # advanced to next cron tick
    else:
        # Not advanced — the schedule can resume from here on reactivation.
        assert schedule.next_run_at == past


@pytest.mark.django_db(transaction=True)
def test_dispatch_resumes_after_owner_reactivated(member_user):
    member_user.is_active = False
    member_user.save(update_fields=["is_active"])
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue

        # Inactive owner: skipped, no run, next_run_at untouched.
        dispatch_scheduled_jobs_cron_task.func()
        assert Run.objects.count() == 0
        schedule.refresh_from_db()
        assert schedule.next_run_at == past

        # Reactivate: fires once and advances to the next cron tick.
        member_user.is_active = True
        member_user.save(update_fields=["is_active"])
        dispatch_scheduled_jobs_cron_task.func()

    assert Run.objects.count() == 1
    schedule.refresh_from_db()
    assert schedule.next_run_at > datetime.now(tz=UTC)


@pytest.mark.django_db(transaction=True)
def test_dispatch_skips_only_inactive_owner_in_mixed_run(member_user):
    inactive_owner = User.objects.create_user(
        username="inactive_owner",
        email="inactive_owner@test.com",
        password="testpass123",  # noqa: S106
        is_active=False,
    )
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    active_schedule = ScheduledJob.objects.create(
        user=member_user,
        name="active",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )
    inactive_schedule = ScheduledJob.objects.create(
        user=inactive_owner,
        name="inactive",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    # Selectivity: only the active owner's schedule dispatched; the inactive
    # owner's was skipped even though both were due in the same run.
    assert Run.objects.count() == 1
    assert Run.objects.filter(session__scheduled_job=active_schedule).count() == 1
    assert Run.objects.filter(session__scheduled_job=inactive_schedule).count() == 0
    active_schedule.refresh_from_db()
    inactive_schedule.refresh_from_db()
    assert active_schedule.next_run_at > past  # advanced
    assert inactive_schedule.next_run_at == past  # untouched


@pytest.mark.django_db(transaction=True)
def test_dispatch_skips_once_schedule_of_inactive_owner_without_retiring(member_user):
    member_user.is_active = False
    member_user.save(update_fields=["is_active"])
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="one-off",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency=Frequency.ONCE,
        run_at=past,
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    # Skipped, not retired: a ONCE schedule owned by an inactive user must keep
    # is_enabled=True and its next_run_at intact so it fires exactly once when
    # the owner is reactivated — not be consumed by the ONCE auto-disable path.
    assert Run.objects.count() == 0
    assert schedule.is_enabled is True
    assert schedule.next_run_at == past
    assert schedule.run_count == 0
