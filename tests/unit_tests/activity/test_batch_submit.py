"""Tests for the multi-repo batch submission service."""

from __future__ import annotations

import uuid
from unittest import mock

import pytest
from activity.models import TriggerType
from activity.services import BatchSubmitFailure, RepoTarget, asubmit_batch_runs, submit_batch_runs
from django_tasks_db.models import DBTaskResult, get_date_max


def _task_result_row(task_id: uuid.UUID) -> mock.Mock:
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
    return mock.Mock(id=task_id)


async def _atask_result_row(task_id: uuid.UUID) -> mock.Mock:
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
    return mock.Mock(id=task_id)


@pytest.mark.django_db(transaction=True)
class TestSubmitBatchRunsSync:
    def test_single_repo_creates_one_activity_with_batch_id(self, member_user):
        task_id = uuid.uuid4()
        fake = _task_result_row(task_id)
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            result = submit_batch_runs(
                user=member_user,
                prompt="do it",
                repos=[RepoTarget(repo_id="a/b", ref="")],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 1
        assert result.failed == []
        assert result.activities[0].batch_id == result.batch_id
        assert result.activities[0].repo_id == "a/b"
        assert result.activities[0].trigger_type == TriggerType.UI_JOB
        m_task.aenqueue.assert_awaited_once()
        enqueue_kwargs = m_task.aenqueue.await_args.kwargs
        assert enqueue_kwargs["repo_id"] == "a/b"
        assert enqueue_kwargs["prompt"] == "do it"
        assert enqueue_kwargs["ref"] is None
        assert enqueue_kwargs["use_max"] is False
        assert enqueue_kwargs["thread_id"] == result.activities[0].thread_id

    def test_five_repos_creates_five_activities_sharing_batch_id(self, member_user):
        tasks_seen = []

        async def _aenqueue(**kwargs):
            tasks_seen.append(kwargs)
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            repos = [RepoTarget(repo_id=f"o/r{i}", ref="dev" if i % 2 else "") for i in range(5)]
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=repos,
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 5
        assert {a.batch_id for a in result.activities} == {result.batch_id}
        assert [t["repo_id"] for t in tasks_seen] == [f"o/r{i}" for i in range(5)]
        assert tasks_seen[0]["ref"] is None  # empty ref threads as None
        assert tasks_seen[1]["ref"] == "dev"
        # Each activity gets a distinct thread_id that matches the one passed to the task.
        activity_thread_ids = [a.thread_id for a in result.activities]
        assert all(activity_thread_ids)
        assert len(set(activity_thread_ids)) == 5
        task_thread_ids = [t["thread_id"] for t in tasks_seen]
        assert set(task_thread_ids) == set(activity_thread_ids)

    def test_oversized_repos_raises_value_error(self, member_user):
        repos = [RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(21)]
        with pytest.raises(ValueError):
            submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=repos,
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

    def test_partial_enqueue_failure_is_best_effort(self, member_user):
        call_count = {"n": 0}

        async def _flaky(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("DB hiccup")
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _flaky
            repos = [
                RepoTarget(repo_id="o/a", ref=""),
                RepoTarget(repo_id="o/b", ref=""),
                RepoTarget(repo_id="o/c", ref=""),
            ]
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=repos,
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 2
        assert len(result.failed) == 1
        failure = result.failed[0]
        assert isinstance(failure, BatchSubmitFailure)
        assert failure.repo_id == "o/b"
        assert "DB hiccup" in failure.error

    def test_orphan_activity_creation_failure_surfaces_in_failed(self, member_user):
        """When enqueue succeeds but acreate_activity raises, the failure is surfaced to the
        caller (not silently dropped) so batch response pairing stays aligned.
        """

        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        class _Stub:
            def __init__(self, task_result_id):
                self.task_result_id = task_result_id
                self.pk = uuid.uuid4()

        async def _flaky_create(**kwargs):
            if kwargs["repo_id"] == "o/b":
                raise RuntimeError("activity INSERT failed")
            return _Stub(task_result_id=kwargs["task_result_id"])

        with (
            mock.patch("activity.services.run_job_task") as m_task,
            mock.patch("activity.services.acreate_activity", side_effect=_flaky_create),
        ):
            m_task.aenqueue = _aenqueue
            repos = [
                RepoTarget(repo_id="o/a", ref=""),
                RepoTarget(repo_id="o/b", ref=""),
                RepoTarget(repo_id="o/c", ref=""),
            ]
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=repos,
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 2
        assert len(result.failed) == 1
        assert result.failed[0].repo_id == "o/b"
        assert "ActivityCreationFailed" in result.failed[0].error

    def test_activity_persists_scheduled_job_link(self, member_user):
        from schedules.models import Frequency, ScheduledJob

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
        )
        fake = _task_result_row(uuid.uuid4())
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="x/y", ref="")],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.SCHEDULE,
                scheduled_job=schedule,
            )
        assert result.activities[0].scheduled_job_id == schedule.pk


@pytest.mark.django_db(transaction=True)
class TestAsubmitBatchRuns:
    async def test_async_variant_returns_same_shape(self, member_user):
        task_id = uuid.uuid4()

        async def _aenqueue(**kwargs):
            return await _atask_result_row(task_id)

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="a/b", ref="")],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.API_JOB,
            )

        assert len(result.activities) == 1
        assert result.activities[0].batch_id == result.batch_id


@pytest.mark.django_db(transaction=True)
class TestBatchTitleEnqueue:
    """One title task per batch — not one per activity."""

    def test_single_batch_title_task_enqueued_for_n_repos(self, member_user, mock_generate_title_task):
        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            repos = [RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(4)]
            result = submit_batch_runs(
                user=member_user,
                prompt="add login",
                repos=repos,
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 4
        mock_generate_title_task.aenqueue.assert_awaited_once()
        call_kwargs = mock_generate_title_task.aenqueue.await_args.kwargs
        assert call_kwargs["batch_id"] == str(result.batch_id)
        assert call_kwargs["prompt"] == "add login"

    def test_no_title_task_for_schedule_trigger(self, member_user, mock_generate_title_task):
        from schedules.models import Frequency, ScheduledJob

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
        )
        fake = _task_result_row(uuid.uuid4())
        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="x/y", ref="")],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.SCHEDULE,
                scheduled_job=schedule,
            )
        mock_generate_title_task.aenqueue.assert_not_called()

    def test_no_title_task_when_no_activities_created(self, member_user, mock_generate_title_task):
        async def _aenqueue_fails(**kwargs):
            raise RuntimeError("queue down")

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue_fails
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="o/r", ref="")],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert result.activities == []
        mock_generate_title_task.aenqueue.assert_not_called()

    def test_title_enqueue_failure_does_not_abort_batch(self, member_user, mock_generate_title_task):
        """Enqueue failures for the (best-effort) title task must not raise to the caller — submission stays green."""
        mock_generate_title_task.aenqueue = mock.AsyncMock(side_effect=RuntimeError("title queue down"))

        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("activity.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            result = submit_batch_runs(
                user=member_user,
                prompt="add login",
                repos=[RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(3)],
                use_max=False,
                notify_on=None,
                trigger_type=TriggerType.UI_JOB,
            )

        assert len(result.activities) == 3
        assert result.failed == []
        mock_generate_title_task.aenqueue.assert_awaited_once()
