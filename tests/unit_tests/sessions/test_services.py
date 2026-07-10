"""Tests for sessions.services — ported from activity/test_batch_submit.py and test_services.py."""

from __future__ import annotations

import uuid
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from notifications.choices import NotifyOn
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.services import (
    BatchSubmitFailure,
    RepoTarget,
    acreate_run,
    aget_or_create_session,
    asubmit_batch_runs,
    submit_batch_runs,
    validate_repo_list,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    return patch("sessions.services.run_job_task", mock_task), mock_task


# ---------------------------------------------------------------------------
# New-behavior tests (from the brief's Step 1)
# ---------------------------------------------------------------------------


async def test_aget_or_create_session_creates_with_origin():
    tid = str(uuid.uuid4())
    session = await aget_or_create_session(thread_id=tid, origin=SessionOrigin.API_JOB, repo_id="g/r", ref="main")
    assert session.thread_id == tid
    assert session.origin == SessionOrigin.API_JOB


async def test_aget_or_create_session_existing_keeps_origin_and_touches():
    tid = str(uuid.uuid4())
    first = await aget_or_create_session(thread_id=tid, origin=SessionOrigin.ISSUE_WEBHOOK, repo_id="g/r")
    before = first.last_active_at
    again = await aget_or_create_session(thread_id=tid, origin=SessionOrigin.API_JOB, repo_id="g/r")
    assert again.pk == first.pk
    assert again.origin == SessionOrigin.ISSUE_WEBHOOK  # first trigger wins
    await again.arefresh_from_db()
    assert again.last_active_at > before


async def test_acreate_run_creates_session_and_run():
    tid = str(uuid.uuid4())
    run = await acreate_run(
        trigger_type=SessionOrigin.ISSUE_WEBHOOK,
        task_result_id=None,
        repo_id="g/r",
        thread_id=tid,
        external_username="gituser",
        prompt="fix the bug",
    )
    assert run.session_id == tid
    session = await Session.objects.aget(pk=tid)
    assert session.origin == SessionOrigin.ISSUE_WEBHOOK
    assert session.external_username == "gituser"


@pytest.mark.django_db(transaction=True)
async def test_submit_batch_creates_session_and_ready_run():
    task_id = uuid.uuid4()
    fake = await _atask_result_row(task_id)
    with patch("sessions.services.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=fake)
        result = await asubmit_batch_runs(
            user=None, prompt="do it", repos=[RepoTarget(repo_id="g/r")], trigger_type=SessionOrigin.API_JOB
        )
    assert len(result.runs) == 1
    run = result.runs[0]
    assert run.status == RunStatus.READY
    assert await Session.objects.filter(pk=run.session_id).aexists()


@pytest.mark.django_db(transaction=True)
async def test_submit_continuation_queues_when_thread_busy():
    tid = str(uuid.uuid4())
    task_id = uuid.uuid4()
    fake = await _atask_result_row(task_id)
    with patch("sessions.services.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=fake)
        first = await asubmit_batch_runs(
            user=None, prompt="p1", repos=[RepoTarget(repo_id="g/r")], trigger_type=SessionOrigin.API_JOB, thread_id=tid
        )
        assert first.runs[0].status == RunStatus.READY
        second = await asubmit_batch_runs(
            user=None, prompt="p2", repos=[RepoTarget(repo_id="g/r")], trigger_type=SessionOrigin.API_JOB, thread_id=tid
        )
    assert second.runs[0].status == RunStatus.QUEUED  # constraint fallback preserved
    assert await Run.objects.filter(session_id=tid).acount() == 2


# ---------------------------------------------------------------------------
# validate_repo_list tests (ported from activity)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Sync batch submit tests (ported from TestSubmitBatchRunsSync)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSubmitBatchRunsSync:
    def test_single_repo_creates_one_run_with_batch_id(self, member_user):
        task_id = uuid.uuid4()
        fake = _task_result_row(task_id)
        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            result = submit_batch_runs(
                user=member_user,
                prompt="do it",
                repos=[RepoTarget(repo_id="a/b", ref="")],
                notify_on=None,
                trigger_type=SessionOrigin.UI_JOB,
            )

        assert len(result.runs) == 1
        assert result.failed == []
        assert result.runs[0].batch_id == result.batch_id
        assert result.runs[0].repo_id == "a/b"
        assert result.runs[0].trigger_type == SessionOrigin.UI_JOB
        m_task.aenqueue.assert_awaited_once()
        enqueue_kwargs = m_task.aenqueue.await_args.kwargs
        assert enqueue_kwargs["repo_id"] == "a/b"
        assert enqueue_kwargs["prompt"] == "do it"
        assert enqueue_kwargs["ref"] is None
        assert enqueue_kwargs["agent_model"] is None
        assert enqueue_kwargs["agent_thinking_level"] is None
        assert "use_max" not in enqueue_kwargs
        assert enqueue_kwargs["thread_id"] == str(result.runs[0].session_id)

    def test_five_repos_creates_five_runs_sharing_batch_id(self, member_user):
        tasks_seen = []

        async def _aenqueue(**kwargs):
            tasks_seen.append(kwargs)
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            repos = [RepoTarget(repo_id=f"o/r{i}", ref="dev" if i % 2 else "") for i in range(5)]
            result = submit_batch_runs(
                user=member_user, prompt="p", repos=repos, notify_on=None, trigger_type=SessionOrigin.UI_JOB
            )

        assert len(result.runs) == 5
        assert {r.batch_id for r in result.runs} == {result.batch_id}
        assert [t["repo_id"] for t in tasks_seen] == [f"o/r{i}" for i in range(5)]
        assert tasks_seen[0]["ref"] is None  # empty ref threads as None
        assert tasks_seen[1]["ref"] == "dev"
        # Each run gets a distinct session_id that matches the one passed to the task.
        run_session_ids = [str(r.session_id) for r in result.runs]
        assert all(run_session_ids)
        assert len(set(run_session_ids)) == 5
        task_thread_ids = [t["thread_id"] for t in tasks_seen]
        assert set(task_thread_ids) == set(run_session_ids)

    def test_oversized_repos_raises_value_error(self, member_user):
        repos = [RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(21)]
        with pytest.raises(ValueError):
            submit_batch_runs(
                user=member_user, prompt="p", repos=repos, notify_on=None, trigger_type=SessionOrigin.UI_JOB
            )

    def test_partial_enqueue_failure_is_best_effort(self, member_user):
        call_count = {"n": 0}

        async def _flaky(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("DB hiccup")
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _flaky
            repos = [
                RepoTarget(repo_id="o/a", ref=""),
                RepoTarget(repo_id="o/b", ref=""),
                RepoTarget(repo_id="o/c", ref=""),
            ]
            result = submit_batch_runs(
                user=member_user, prompt="p", repos=repos, notify_on=None, trigger_type=SessionOrigin.UI_JOB
            )

        assert len(result.runs) == 2
        assert len(result.failed) == 1
        failure = result.failed[0]
        assert isinstance(failure, BatchSubmitFailure)
        assert failure.repo_id == "o/b"
        assert "DB hiccup" in failure.error

    def test_run_creation_failure_uses_run_creation_failed_prefix(self, member_user):
        """RunCreationFailed: prefix is used when acreate_run itself raises."""
        call_count = {"n": 0}

        async def _flaky_create(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("constraint violation")
            # Delegate to real acreate_run for other calls.
            return await acreate_run(**kwargs)

        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        patch_create = mock.patch("sessions.services.acreate_run", side_effect=_flaky_create)
        patch_task, m_task = _patch_run_job_task()
        m_task.aenqueue = _aenqueue
        with patch_create, patch_task:
            repos = [RepoTarget(repo_id="o/a", ref=""), RepoTarget(repo_id="o/b", ref="")]
            result = submit_batch_runs(
                user=member_user, prompt="p", repos=repos, notify_on=None, trigger_type=SessionOrigin.UI_JOB
            )

        assert len(result.runs) == 1
        assert len(result.failed) == 1
        failure = result.failed[0]
        assert failure.repo_id == "o/b"
        assert failure.error.startswith("RunCreationFailed:")
        assert "constraint violation" in failure.error

    def test_run_persists_scheduled_job_link(self, member_user):
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
        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="x/y", ref="")],
                notify_on=None,
                trigger_type=SessionOrigin.SCHEDULE,
                scheduled_job=schedule,
            )
        # The run's session should have the scheduled_job linked
        session = Session.objects.get(pk=result.runs[0].session_id)
        assert session.scheduled_job_id == schedule.pk


# ---------------------------------------------------------------------------
# Async batch submit tests (ported from TestAsubmitBatchRuns)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAsubmitBatchRuns:
    async def test_async_variant_returns_same_shape(self, member_user):
        task_id = uuid.uuid4()

        async def _aenqueue(**kwargs):
            return await _atask_result_row(task_id)

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="a/b", ref="")],
                notify_on=None,
                trigger_type=SessionOrigin.API_JOB,
            )

        assert len(result.runs) == 1
        assert result.runs[0].batch_id == result.batch_id


# ---------------------------------------------------------------------------
# Batch title task tests (ported from TestBatchTitleEnqueue)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestBatchTitleEnqueue:
    """One title task per batch — not one per run."""

    def test_single_batch_title_task_enqueued_for_n_repos(self, member_user, mock_generate_title_task):
        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            repos = [RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(4)]
            result = submit_batch_runs(
                user=member_user, prompt="add login", repos=repos, notify_on=None, trigger_type=SessionOrigin.UI_JOB
            )

        assert len(result.runs) == 4
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
        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = mock.AsyncMock(return_value=fake)
            submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="x/y", ref="")],
                notify_on=None,
                trigger_type=SessionOrigin.SCHEDULE,
                scheduled_job=schedule,
            )
        mock_generate_title_task.aenqueue.assert_not_called()

    def test_no_title_task_when_no_runs_created(self, member_user, mock_generate_title_task):
        async def _aenqueue_fails(**kwargs):
            raise RuntimeError("queue down")

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue_fails
            result = submit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="o/r", ref="")],
                notify_on=None,
                trigger_type=SessionOrigin.UI_JOB,
            )

        assert result.runs == []
        mock_generate_title_task.aenqueue.assert_not_called()

    def test_title_enqueue_failure_does_not_abort_batch(self, member_user, mock_generate_title_task):
        """Enqueue failures for the (best-effort) title task must not raise to the caller."""
        mock_generate_title_task.aenqueue = mock.AsyncMock(side_effect=RuntimeError("title queue down"))

        async def _aenqueue(**kwargs):
            return await _atask_result_row(uuid.uuid4())

        with mock.patch("sessions.services.run_job_task") as m_task:
            m_task.aenqueue = _aenqueue
            result = submit_batch_runs(
                user=member_user,
                prompt="add login",
                repos=[RepoTarget(repo_id=f"o/r{i}", ref="") for i in range(3)],
                notify_on=None,
                trigger_type=SessionOrigin.UI_JOB,
            )

        assert len(result.runs) == 3
        assert result.failed == []
        mock_generate_title_task.aenqueue.assert_awaited_once()


# ---------------------------------------------------------------------------
# Thread/session continuation tests (ported from TestThreadContinuation)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestSessionContinuation:
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
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread,
            )
        run = result.runs[0]
        assert str(run.session_id) == thread

    async def test_multi_repo_with_thread_id_raises(self, member_user):
        thread = str(uuid.uuid4())
        with pytest.raises(ValueError, match="exactly one repo"):
            await asubmit_batch_runs(
                user=member_user,
                prompt="p",
                repos=[RepoTarget(repo_id="a/b", ref=""), RepoTarget(repo_id="c/d", ref="")],
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread,
            )

    async def test_prior_terminal_creates_ready_and_enqueues(self, member_user):
        thread = str(uuid.uuid4())
        # Create the session and a prior terminal Run
        session = await Session.objects.acreate(thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="acme/api")
        await Run.objects.acreate(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="acme/api",
            status=RunStatus.SUCCESSFUL,
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
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread,
            )
        run = result.runs[0]
        assert run.status == RunStatus.READY
        mock_task.aenqueue.assert_called_once()

    async def test_prior_non_terminal_creates_queued_and_skips_enqueue(self, member_user):
        thread = str(uuid.uuid4())
        session = await Session.objects.acreate(thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="acme/api")
        await Run.objects.acreate(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="acme/api",
            status=RunStatus.RUNNING,
            user=member_user,
        )
        patcher, mock_task = _patch_run_job_task()
        with patcher:
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread,
            )
        run = result.runs[0]
        assert run.status == RunStatus.QUEUED
        assert run.task_result_id is None
        mock_task.aenqueue.assert_not_called()

    async def test_asubmit_batch_runs_stores_and_forwards_overrides(self, member_user):
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
                trigger_type=SessionOrigin.UI_JOB,
            )

        assert result.runs[0].agent_model == "openrouter:anthropic/claude-haiku-4.5"
        assert result.runs[0].agent_thinking_level == "low"
        enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
        assert enqueue_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
        assert enqueue_kwargs["agent_thinking_level"] == "low"
        assert "use_max" not in enqueue_kwargs

    async def test_asubmit_batch_runs_empty_overrides_pass_none_to_aenqueue(self, member_user):
        fake_task = await _make_db_task_result()
        patcher, mock_task = _patch_run_job_task()
        mock_task.aenqueue.return_value = fake_task
        with patcher:
            await asubmit_batch_runs(
                user=member_user,
                prompt="do thing",
                repos=[RepoTarget(repo_id="acme/x", ref="")],
                trigger_type=SessionOrigin.UI_JOB,
            )

        enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
        assert enqueue_kwargs["agent_model"] is None
        assert enqueue_kwargs["agent_thinking_level"] is None
        assert "use_max" not in enqueue_kwargs

    async def test_enqueue_failure_marks_failed_with_audit_and_releases_queued_sibling(self, member_user):
        """When enqueue raises, run transitions to FAILED and queued sibling is released."""
        thread = str(uuid.uuid4())
        # A prior QUEUED sibling waiting for the active slot to open up.
        session = await Session.objects.acreate(thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="acme/api")
        queued = await Run.objects.acreate(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="acme/api",
            status=RunStatus.QUEUED,
            user=member_user,
            prompt="p",
        )
        good_task = await _make_db_task_result()
        services_patch, services_mock = _patch_run_job_task(side_effect=RuntimeError("broker down"))
        signals_mock = MagicMock()
        signals_mock.aenqueue = AsyncMock(return_value=good_task)
        with services_patch, patch("sessions.signals.run_job_task", signals_mock):
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="acme/api", ref="")],
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread,
            )

        assert result.runs == [] and len(result.failed) == 1
        assert "RuntimeError" in result.failed[0].error
        services_mock.aenqueue.assert_awaited_once()

        failed_row = await Run.objects.aget(session_id=thread, status=RunStatus.FAILED)
        assert failed_row.error_message.startswith("enqueue_failed:")
        assert failed_row.finished_at is not None

        await queued.arefresh_from_db()
        assert queued.status == RunStatus.READY
        assert queued.task_result_id == good_task.id


# ---------------------------------------------------------------------------
# notify_on tests (ported from TestCreateActivityNotifyOn / TestEffectiveNotifyOn)
# ---------------------------------------------------------------------------


class TestCreateRunNotifyOn:
    def test_explicit_notify_on_is_persisted(self, member_user):

        # Need a session first
        session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="x/y")
        run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.UI_JOB,
            repo_id="x/y",
            user=member_user,
            notify_on=NotifyOn.NEVER,
        )
        assert run.notify_on == NotifyOn.NEVER

    def test_no_notify_on_leaves_null(self, member_user):
        session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="x/y")
        run = Run.objects.create(session=session, trigger_type=SessionOrigin.UI_JOB, repo_id="x/y", user=member_user)
        assert run.notify_on is None
