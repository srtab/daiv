import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from django.db import DatabaseError

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from notifications.choices import EventType
from notifications.models import Notification
from sessions.models import Run, SessionOrigin

from accounts.models import User
from codebase.authorization import RepositoryAccessDenied
from schedules.models import DispatchError, Frequency, ScheduledJob
from schedules.tasks import dispatch_scheduled_jobs_cron_task


def _make_task_result() -> MagicMock:
    """Build a real DBTaskResult row so ``create_activity`` can link to it via FK."""
    task_id = uuid.uuid4()
    DBTaskResult.objects.create(
        id=task_id,
        status="READY",
        task_path="sessions.executor.tasks.run_job_task",
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
        task_path="sessions.executor.tasks.run_job_task",
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
    assert Run.objects.filter(session__scheduled_job=schedule).count() == expected_runs
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
        assert Run.objects.filter(session__scheduled_job=schedule).count() == 0
        schedule.refresh_from_db()
        assert schedule.next_run_at == past

        # Reactivate: fires once and advances to the next cron tick.
        member_user.is_active = True
        member_user.save(update_fields=["is_active"])
        dispatch_scheduled_jobs_cron_task.func()

    assert Run.objects.filter(session__scheduled_job=schedule).count() == 1
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
    assert Run.objects.filter(session__scheduled_job__in=[active_schedule, inactive_schedule]).count() == 1
    assert Run.objects.filter(session__scheduled_job=active_schedule).count() == 1
    assert Run.objects.filter(session__scheduled_job=inactive_schedule).count() == 0
    active_schedule.refresh_from_db()
    inactive_schedule.refresh_from_db()
    assert active_schedule.next_run_at > past  # advanced
    assert inactive_schedule.next_run_at == past  # untouched


@pytest.mark.django_db(transaction=True)
def test_dispatch_stamps_schedule_mcp_overrides_onto_session(member_user):
    past = datetime.now(tz=UTC) - timedelta(minutes=1)
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="daily",
        prompt="do stuff",
        repos=[{"repo_id": "acme/repo", "ref": ""}],
        frequency="daily",
        time="09:00",
        mcp_overrides={"a": "off"},
        is_enabled=True,
        next_run_at=past,
    )

    with patch("sessions.services.run_job_task") as mock_task:

        async def _aenqueue(**kwargs):
            return await _amake_task_result()

        mock_task.aenqueue.side_effect = _aenqueue
        dispatch_scheduled_jobs_cron_task.func()

    from sessions.models import Session

    session = Session.objects.get(scheduled_job=schedule)
    assert session.mcp_overrides == {"a": "off"}


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
    assert Run.objects.filter(session__scheduled_job=schedule).count() == 0
    assert schedule.is_enabled is True
    assert schedule.next_run_at == past
    assert schedule.run_count == 0


async def _aenqueue_ok(**kwargs):
    return await _amake_task_result()


def _due_daily(user, **fields):
    return ScheduledJob.objects.create(
        user=user,
        name=fields.pop("name", "daily"),
        prompt="do stuff",
        repos=fields.pop("repos", [{"repo_id": "acme/repo", "ref": ""}]),
        frequency="daily",
        time="09:00",
        is_enabled=True,
        next_run_at=datetime.now(tz=UTC) - timedelta(minutes=1),
        **fields,
    )


def _make_due(schedule):
    ScheduledJob.objects.filter(pk=schedule.pk).update(next_run_at=datetime.now(tz=UTC) - timedelta(minutes=1))


def _dispatch(*, denied=None, aenqueue=_aenqueue_ok):
    with (
        patch("sessions.services.aassert_can_run", new=AsyncMock(side_effect=denied, return_value=None)),
        patch("sessions.services.run_job_task") as mock_task,
    ):
        mock_task.aenqueue.side_effect = aenqueue
        dispatch_scheduled_jobs_cron_task.func()


def _failure_notices():
    return Notification.objects.filter(event_type=EventType.SCHEDULE_DISPATCH_FAILED)


@pytest.mark.django_db(transaction=True)
def test_dispatch_unexpected_error_records_the_failure_and_notifies(member_user):
    schedule = _due_daily(member_user)

    with patch("sessions.services.submit_batch_runs", side_effect=RuntimeError("boom")):
        dispatch_scheduled_jobs_cron_task.func()

    schedule.refresh_from_db()
    assert schedule.dispatch_error == DispatchError.UNEXPECTED
    assert schedule.dispatch_error_repo_ids == []
    assert schedule.failing_since is not None
    assert list(_failure_notices().values_list("recipient", flat=True)) == [member_user.pk]


@pytest.mark.django_db(transaction=True)
def test_dispatch_denied_records_the_failure_without_counting_a_run(member_user):
    schedule = _due_daily(member_user)
    before = datetime.now(tz=UTC)

    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    schedule.refresh_from_db()
    assert schedule.failing_since >= before
    assert schedule.dispatch_error == DispatchError.REPO_ACCESS_DENIED
    assert schedule.dispatch_error_repo_ids == ["acme/repo"]
    assert (schedule.last_run_at, schedule.run_count) == (None, 0)


@pytest.mark.django_db(transaction=True)
def test_a_repeat_denial_keeps_the_streak_and_notifies_once(member_user):
    schedule = _due_daily(member_user)
    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))
    schedule.refresh_from_db()
    streak_start = schedule.failing_since

    _make_due(schedule)
    _dispatch(denied=RepositoryAccessDenied(["acme/other"]))

    schedule.refresh_from_db()
    assert schedule.dispatch_error_repo_ids == ["acme/other"]
    assert schedule.failing_since == streak_start
    assert list(_failure_notices().values_list("recipient", flat=True)) == [member_user.pk]


@pytest.mark.django_db(transaction=True)
def test_successful_dispatch_ends_the_failing_streak(member_user):
    schedule = _due_daily(
        member_user,
        failing_since=datetime.now(tz=UTC) - timedelta(days=3),
        dispatch_error=DispatchError.REPO_ACCESS_DENIED,
        dispatch_error_repo_ids=["acme/repo"],
    )

    _dispatch()

    schedule.refresh_from_db()
    assert schedule.run_count == 1
    assert (schedule.failing_since, schedule.dispatch_error, schedule.dispatch_error_repo_ids) == (None, "", [])


@pytest.mark.django_db(transaction=True)
def test_a_batch_whose_every_repo_fails_to_enqueue_is_a_failed_dispatch(member_user):
    schedule = _due_daily(member_user)

    async def _broker_down(**kwargs):
        raise RuntimeError("broker down")

    _dispatch(aenqueue=_broker_down)

    schedule.refresh_from_db()
    assert schedule.dispatch_error == DispatchError.UNEXPECTED
    assert schedule.failing_since is not None
    assert (schedule.run_count, schedule.last_run_at, schedule.last_run_batch_id) == (0, None, None)
    assert schedule.next_run_at > datetime.now(tz=UTC)
    assert _failure_notices().count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_batch_where_some_repos_start_still_ends_the_failing_streak(member_user):
    schedule = _due_daily(
        member_user,
        repos=[{"repo_id": "acme/ok", "ref": ""}, {"repo_id": "acme/broken", "ref": ""}],
        failing_since=datetime.now(tz=UTC) - timedelta(days=1),
        dispatch_error=DispatchError.UNEXPECTED,
    )

    async def _aenqueue(**kwargs):
        if kwargs["repo_id"] == "acme/broken":
            raise RuntimeError("broker hiccup")
        return await _amake_task_result()

    _dispatch(aenqueue=_aenqueue)

    schedule.refresh_from_db()
    assert schedule.run_count == 1
    assert schedule.failing_since is None


@pytest.mark.django_db(transaction=True)
def test_a_schedule_that_cannot_advance_is_disabled_with_its_failure_on_record(member_user):
    schedule = _due_daily(member_user)

    with patch.object(ScheduledJob, "advance_after_dispatch", side_effect=ValueError("bad cron")):
        _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    schedule.refresh_from_db()
    assert (schedule.is_enabled, schedule.next_run_at) == (False, None)
    assert schedule.dispatch_error == DispatchError.REPO_ACCESS_DENIED
    assert schedule.dispatch_error_repo_ids == ["acme/repo"]
    assert schedule.failing_since is not None
    assert _failure_notices().count() == 1


@pytest.mark.django_db(transaction=True)
def test_disabling_keeps_the_streak_start_of_an_already_failing_schedule(member_user):
    streak_start = datetime.now(tz=UTC) - timedelta(days=2)
    schedule = _due_daily(member_user, failing_since=streak_start, dispatch_error=DispatchError.UNEXPECTED)

    with patch.object(ScheduledJob, "advance_after_dispatch", side_effect=ValueError("bad cron")):
        _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    schedule.refresh_from_db()
    assert schedule.is_enabled is False
    assert schedule.failing_since == streak_start


@pytest.mark.django_db(transaction=True)
def test_a_database_error_recording_one_failure_keeps_the_other_dispatches(member_user):
    denied = _due_daily(member_user, name="denied", repos=[{"repo_id": "acme/denied", "ref": ""}])
    healthy = _due_daily(member_user, name="healthy", repos=[{"repo_id": "acme/ok", "ref": ""}])

    async def can_run(user, repo_ids):
        if "acme/denied" in repo_ids:
            raise RepositoryAccessDenied(["acme/denied"])

    original = ScheduledJob._do_update

    def _do_update(self, base_qs, using, pk_val, values, update_fields, *args, **kwargs):
        if self.pk == denied.pk:
            raise DatabaseError("statement timeout")
        return original(self, base_qs, using, pk_val, values, update_fields, *args, **kwargs)

    with patch.object(ScheduledJob, "_do_update", _do_update):
        _dispatch(denied=can_run)

    healthy.refresh_from_db()
    denied.refresh_from_db()
    assert healthy.run_count == 1
    assert denied.is_enabled is False
    assert denied.dispatch_error == DispatchError.REPO_ACCESS_DENIED


@pytest.mark.django_db(transaction=True)
def test_each_failing_owner_gets_their_own_notice(member_user, admin_user):
    _due_daily(member_user, name="a")
    _due_daily(admin_user, name="b")

    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    assert sorted(_failure_notices().values_list("recipient", flat=True)) == sorted([member_user.pk, admin_user.pk])


@pytest.mark.django_db(transaction=True)
def test_a_notice_lost_to_an_error_is_sent_on_the_next_failure(member_user):
    schedule = _due_daily(member_user)

    with patch("notifications.schedule_notifiers._render_payload", side_effect=RuntimeError("render failed")):
        _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))
    assert not _failure_notices().exists()

    _make_due(schedule)
    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    assert _failure_notices().count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_schedule_that_recovers_and_fails_again_notifies_twice(member_user):
    schedule = _due_daily(member_user)

    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))
    _make_due(schedule)
    _dispatch()
    schedule.refresh_from_db()
    assert schedule.failing_since is None
    _make_due(schedule)
    _dispatch(denied=RepositoryAccessDenied(["acme/repo"]))

    assert _failure_notices().count() == 2
