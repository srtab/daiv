import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.services import RepoTarget, acreate_activity, asubmit_batch_runs, create_activity, validate_repo_list
from django_tasks_db.models import DBTaskResult, get_date_max
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob


class TestValidateRepoListDuplicates:
    def test_duplicate_with_ref_mentions_both_repo_and_ref(self):
        raw = [{"repo_id": "acme/api", "ref": "main"}, {"repo_id": "acme/api", "ref": "main"}]
        with pytest.raises(ValueError) as exc:
            validate_repo_list(raw)
        msg = str(exc.value)
        assert "acme/api" in msg
        assert "main" in msg

    def test_duplicate_without_ref_omits_on_clause(self):
        raw = [{"repo_id": "acme/api", "ref": ""}, {"repo_id": "acme/api", "ref": ""}]
        with pytest.raises(ValueError) as exc:
            validate_repo_list(raw)
        msg = str(exc.value)
        assert "acme/api" in msg
        assert " on " not in msg


@pytest.mark.django_db
class TestCreateActivityNotifyOn:
    def test_explicit_notify_on_is_persisted(self, member_user):
        activity = create_activity(
            trigger_type=TriggerType.UI_JOB,
            task_result_id=None,
            repo_id="x/y",
            user=member_user,
            notify_on=NotifyOn.NEVER,
        )
        assert activity.notify_on == NotifyOn.NEVER

    def test_no_notify_on_leaves_null(self, member_user):
        activity = create_activity(
            trigger_type=TriggerType.UI_JOB, task_result_id=None, repo_id="x/y", user=member_user
        )
        assert activity.notify_on is None

    def test_schedule_run_defers_to_schedule_when_no_override(self, member_user):
        """Without an explicit override, activity.notify_on stays null and the effective
        value falls through to ScheduledJob.notify_on via Activity.effective_notify_on."""
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ALWAYS,
        )
        activity = create_activity(
            trigger_type=TriggerType.SCHEDULE,
            task_result_id=None,
            repo_id="x/y",
            scheduled_job=schedule,
            user=member_user,
        )
        assert activity.notify_on is None
        assert activity.effective_notify_on == NotifyOn.ALWAYS

    def test_explicit_notify_on_beats_schedule_default(self, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ALWAYS,
        )
        activity = create_activity(
            trigger_type=TriggerType.SCHEDULE,
            task_result_id=None,
            repo_id="x/y",
            scheduled_job=schedule,
            user=member_user,
            notify_on=NotifyOn.NEVER,
        )
        assert activity.notify_on == NotifyOn.NEVER
        assert activity.effective_notify_on == NotifyOn.NEVER


@pytest.mark.django_db(transaction=True)
class TestAcreateActivityNotifyOn:
    async def test_async_variant_threads_notify_on(self, member_user):
        activity = await acreate_activity(
            trigger_type=TriggerType.API_JOB,
            task_result_id=None,
            repo_id="x/y",
            user=member_user,
            notify_on=NotifyOn.ON_FAILURE,
        )
        assert activity.notify_on == NotifyOn.ON_FAILURE


@pytest.mark.django_db
class TestEffectiveNotifyOn:
    def test_run_override_wins_over_user_default(self, member_user):
        from activity.models import Activity

        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y", notify_on=NotifyOn.ALWAYS
        )
        assert activity.effective_notify_on == NotifyOn.ALWAYS

    def test_falls_back_to_user_default_when_no_override(self, member_user):
        from activity.models import Activity

        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y")
        assert activity.effective_notify_on == NotifyOn.ON_FAILURE

    def test_schedule_default_used_when_no_override(self, member_user):
        from activity.models import Activity

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ON_SUCCESS,
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, user=member_user, repo_id="x/y", scheduled_job=schedule
        )
        assert activity.effective_notify_on == NotifyOn.ON_SUCCESS

    def test_returns_never_when_no_user_no_schedule_no_override(self):
        from activity.models import Activity

        activity = Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="x/y")
        assert activity.effective_notify_on == NotifyOn.NEVER


async def _make_db_task_result() -> MagicMock:
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


def _patch_run_job_task(side_effect=None):
    mock_task = MagicMock()
    mock_task.aenqueue = AsyncMock(side_effect=side_effect)
    return patch("activity.services.run_job_task", mock_task), mock_task


@pytest.mark.django_db(transaction=True)
class TestThreadContinuation:
    async def test_reuses_supplied_thread_id(self, member_user):
        thread = str(uuid.uuid4())
        fake_task = await _make_db_task_result()
        patcher, mock_task = _patch_run_job_task()
        mock_task.aenqueue.return_value = fake_task
        with patcher:
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=TriggerType.API_JOB,
                thread_id=thread,
            )
        activity = result.activities[0]
        assert activity.thread_id == thread

    async def test_multi_repo_with_thread_id_raises(self, member_user):
        thread = str(uuid.uuid4())
        with pytest.raises(ValueError, match="exactly one repo"):
            await asubmit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="a/b", ref=""), RepoTarget(repo_id="c/d", ref="")],
                trigger_type=TriggerType.API_JOB,
                thread_id=thread,
            )

    async def test_prior_terminal_creates_ready_and_enqueues(self, member_user):
        thread = str(uuid.uuid4())
        # Prior terminal Activity on this thread
        await Activity.objects.acreate(
            trigger_type=TriggerType.API_JOB,
            repo_id="acme/api",
            thread_id=thread,
            status=ActivityStatus.SUCCESSFUL,
            user=member_user,
        )
        fake_task = await _make_db_task_result()
        patcher, mock_task = _patch_run_job_task()
        mock_task.aenqueue.return_value = fake_task
        with patcher:
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=TriggerType.API_JOB,
                thread_id=thread,
            )
        activity = result.activities[0]
        assert activity.status == ActivityStatus.READY
        mock_task.aenqueue.assert_called_once()

    async def test_prior_non_terminal_creates_queued_and_skips_enqueue(self, member_user):
        thread = str(uuid.uuid4())
        await Activity.objects.acreate(
            trigger_type=TriggerType.API_JOB,
            repo_id="acme/api",
            thread_id=thread,
            status=ActivityStatus.RUNNING,
            user=member_user,
        )
        patcher, mock_task = _patch_run_job_task()
        with patcher:
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=TriggerType.API_JOB,
                thread_id=thread,
            )
        activity = result.activities[0]
        assert activity.status == ActivityStatus.QUEUED
        assert activity.task_result_id is None
        mock_task.aenqueue.assert_not_called()

    async def test_asubmit_batch_runs_stores_and_forwards_overrides(self, member_user):
        """The override pair must round-trip onto Activity and onto run_job_task.aenqueue.

        Empty-string defaults are converted to ``None`` at the ``run_job_task.aenqueue``
        boundary (matching its ``str | None`` signature); explicit values flow through
        unchanged and ``use_max`` must NOT be forwarded.
        """
        fake_task = await _make_db_task_result()
        patcher, mock_task = _patch_run_job_task()
        mock_task.aenqueue.return_value = fake_task
        with patcher:
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="do thing",
                repos=[RepoTarget(repo_id="acme/x", ref="main")],
                agent_model="openrouter:anthropic/claude-haiku-4.5",
                agent_thinking_level="low",
                trigger_type=TriggerType.UI_JOB,
            )

        assert result.activities[0].agent_model == "openrouter:anthropic/claude-haiku-4.5"
        assert result.activities[0].agent_thinking_level == "low"
        enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
        assert enqueue_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
        assert enqueue_kwargs["agent_thinking_level"] == "low"
        assert "use_max" not in enqueue_kwargs

    async def test_asubmit_batch_runs_empty_overrides_pass_none_to_aenqueue(self, member_user):
        """Default empty-string overrides must surface as ``None`` at the task boundary."""
        fake_task = await _make_db_task_result()
        patcher, mock_task = _patch_run_job_task()
        mock_task.aenqueue.return_value = fake_task
        with patcher:
            await asubmit_batch_runs(
                user=member_user,
                prompt="do thing",
                repos=[RepoTarget(repo_id="acme/x", ref="")],
                trigger_type=TriggerType.UI_JOB,
            )

        enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
        assert enqueue_kwargs["agent_model"] is None
        assert enqueue_kwargs["agent_thinking_level"] is None
        assert "use_max" not in enqueue_kwargs

    async def test_enqueue_failure_marks_failed_with_audit_and_releases_queued_sibling(self, member_user):
        """When the new READY row's enqueue raises, the row must transition to FAILED with
        ``finished_at`` and a ``enqueue_failed:`` error_message, and an existing QUEUED sibling
        on the same thread must be released (via emit_activity_finished_if_terminal)."""
        thread = str(uuid.uuid4())
        # A prior QUEUED sibling waiting for the active slot to open up.
        queued = await Activity.objects.acreate(
            trigger_type=TriggerType.API_JOB,
            repo_id="acme/api",
            thread_id=thread,
            status=ActivityStatus.QUEUED,
            user=member_user,
            prompt="p",
        )
        good_task = await _make_db_task_result()
        # services-layer aenqueue fails (the new submission); the dispatcher-layer
        # aenqueue (used by signals.py to release the QUEUED sibling) succeeds.
        services_patch, services_mock = _patch_run_job_task(side_effect=RuntimeError("broker down"))
        signals_mock = MagicMock()
        signals_mock.aenqueue = AsyncMock(return_value=good_task)
        with services_patch, patch("activity.signals.run_job_task", signals_mock):
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=TriggerType.API_JOB,
                thread_id=thread,
            )

        assert result.activities == [] and len(result.failed) == 1
        assert "RuntimeError" in result.failed[0].error
        services_mock.aenqueue.assert_awaited_once()

        failed_row = await Activity.objects.aget(thread_id=thread, status=ActivityStatus.FAILED)
        assert failed_row.error_message.startswith("enqueue_failed:")
        assert failed_row.finished_at is not None

        await queued.arefresh_from_db()
        assert queued.status == ActivityStatus.READY
        assert queued.task_result_id == good_task.id
