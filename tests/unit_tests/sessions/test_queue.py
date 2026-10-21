import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from sandbox_envs.models import SandboxEnvironment, Visibility
from sessions.models import Run, RunStatus, SessionOrigin
from sessions.queue import (
    LINK_FAILED_PREFIX,
    MAX_CONSECUTIVE_DISPATCH_FAILURES,
    EnqueueFailure,
    aenqueue_run,
    amark_failed_and_advance,
    dispatch_next_queued,
    release_orphan_queued_runs,
    release_runs_waiting_for,
)

from tests.unit_tests.sessions.conftest import make_run


def _emits(spy: MagicMock) -> list[tuple[str, bool, bool]]:
    """Every ``run_finished`` emit, in order, as a ``(run id, skip_dispatch, never_started)`` triple."""
    return [
        (str(call.kwargs["run"].pk), call.kwargs["skip_dispatch"], call.kwargs["never_started"])
        for call in spy.call_args_list
    ]


@pytest.mark.django_db(transaction=True)
class TestAenqueueRun:
    @pytest.mark.parametrize(
        ("trigger_type", "ask_user_enabled", "fork_branch"),
        [
            (SessionOrigin.API_JOB, True, False),
            (SessionOrigin.SCHEDULE, False, False),
            (SessionOrigin.PIPELINE_WEBHOOK, False, False),
            (SessionOrigin.DELEGATED_JOB, False, True),
        ],
    )
    async def test_the_task_kwargs_come_from_the_row(
        self, create_db_task_result, member_user, trigger_type, ask_user_enabled, fork_branch
    ):
        env = await SandboxEnvironment.objects.acreate(scope=Visibility.GLOBAL, name="ci", base_image="python:3.14")
        run = await sync_to_async(make_run)(
            session_id=str(uuid.uuid4()),
            status=RunStatus.READY,
            trigger_type=trigger_type,
            ref="feat/x",
            agent_model="openrouter:anthropic/claude-opus-4.6",
            agent_thinking_level="high",
            sandbox_environment=env,
            user=member_user,
        )
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            assert await aenqueue_run(run, log_context="test") is None

        assert mock_task.aenqueue.call_args.kwargs == {
            "repo_id": "acme/api",
            "prompt": "p",
            "thread_id": run.session_id,
            "ref": "feat/x",
            "agent_model": "openrouter:anthropic/claude-opus-4.6",
            "agent_thinking_level": "high",
            "sandbox_environment_id": str(env.id),
            "run_id": str(run.pk),
            "user_id": member_user.pk,
            "ask_user_enabled": ask_user_enabled,
            "fork_branch": fork_branch,
        }
        await run.arefresh_from_db()
        assert run.task_result_id == task.id

    async def test_blank_row_fields_reach_the_task_as_none(self, create_db_task_result):
        run = await sync_to_async(make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            await aenqueue_run(run, log_context="test")

        kwargs = mock_task.aenqueue.call_args.kwargs
        assert (kwargs["ref"], kwargs["agent_model"], kwargs["agent_thinking_level"]) == (None, None, None)
        assert (kwargs["sandbox_environment_id"], kwargs["user_id"]) == (None, None)

    @pytest.mark.parametrize("skip_dispatch", [False, True])
    async def test_a_failed_enqueue_fails_the_row_and_emits(self, run_finished_spy, skip_dispatch):
        run = await sync_to_async(make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("queue down"))
            failure = await aenqueue_run(run, log_context="test", skip_dispatch=skip_dispatch)

        assert failure == EnqueueFailure(task_enqueued=False)
        await run.arefresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.error_message == "enqueue_failed: RuntimeError: queue down"
        assert run.finished_at is not None
        assert run.task_result_id is None
        assert _emits(run_finished_spy) == [(str(run.pk), skip_dispatch, True)]

    async def test_a_failed_link_fails_the_row_with_the_link_prefix_and_emits(self, run_finished_spy):
        run = await sync_to_async(make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))  # no such task result row
            failure = await aenqueue_run(run, log_context="test")

        assert failure is not None
        assert failure.task_enqueued is True
        await run.arefresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.error_message.startswith(f"{LINK_FAILED_PREFIX}: IntegrityError")
        assert run.task_result_id is None
        assert _emits(run_finished_spy) == [(str(run.pk), False, True)]


@pytest.mark.django_db(transaction=True)
class TestAmarkFailedAndAdvance:
    async def test_a_row_that_got_running_meanwhile_keeps_its_status(self, run_finished_spy):
        """A link that raised after it committed lets the worker move the row on; failing it then would hide the
        result of a run that is going ahead."""
        run = await sync_to_async(make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.RUNNING)

        await amark_failed_and_advance(run, prefix=LINK_FAILED_PREFIX, err=RuntimeError("x"), log_context="test")

        assert run.error_message == f"{LINK_FAILED_PREFIX}: RuntimeError: x"
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.RUNNING, "")
        run_finished_spy.assert_not_called()

    async def test_a_failed_write_is_logged_without_emitting(self, run_finished_spy, caplog):
        run = await sync_to_async(make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch.object(Run.objects, "afinish", AsyncMock(side_effect=RuntimeError("db down"))):
            await amark_failed_and_advance(run, prefix="enqueue_failed", err=RuntimeError("x"), log_context="test")

        assert f"test: failed to mark run={run.pk} FAILED" in caplog.text
        run_finished_spy.assert_not_called()


@pytest.mark.django_db(transaction=True)
class TestDispatchNextQueued:
    def test_releases_the_oldest_queued_sibling(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        oldest = make_run(session_id=session_id, status=RunStatus.QUEUED)
        newer = make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            dispatch_next_queued(session_id)

        oldest.refresh_from_db()
        newer.refresh_from_db()
        assert (oldest.status, oldest.task_result_id) == (RunStatus.READY, fake_task.id)
        assert newer.status == RunStatus.QUEUED

    def test_no_op_when_no_queued_sibling(self):
        session_id = str(uuid.uuid4())
        make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            dispatch_next_queued(session_id)
        mock_task.aenqueue.assert_not_called()

    def test_a_failed_enqueue_fails_the_row_and_moves_to_the_next(self, create_db_task_result, run_finished_spy):
        session_id = str(uuid.uuid4())
        bad = make_run(session_id=session_id, status=RunStatus.QUEUED)
        next_q = make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=[RuntimeError("queue down"), fake_task])
            dispatch_next_queued(session_id)

        bad.refresh_from_db()
        next_q.refresh_from_db()
        assert bad.status == RunStatus.FAILED
        assert bad.error_message == "enqueue_failed: RuntimeError: queue down"
        assert (next_q.status, next_q.task_result_id) == (RunStatus.READY, fake_task.id)
        # The emit must not re-enter the dispatcher, which moves on by itself.
        assert _emits(run_finished_spy) == [(str(bad.pk), True, True)]

    def test_a_failed_link_fails_the_row_and_moves_to_the_next(self, create_db_task_result, run_finished_spy):
        session_id = str(uuid.uuid4())
        unlinked = make_run(session_id=session_id, status=RunStatus.QUEUED)
        next_q = make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=[MagicMock(id=uuid.uuid4()), fake_task])
            dispatch_next_queued(session_id)

        unlinked.refresh_from_db()
        next_q.refresh_from_db()
        assert unlinked.status == RunStatus.FAILED
        assert unlinked.error_message.startswith(LINK_FAILED_PREFIX)
        assert (next_q.status, next_q.task_result_id) == (RunStatus.READY, fake_task.id)
        assert _emits(run_finished_spy) == [(str(unlinked.pk), True, True)]

    def test_bails_after_max_consecutive_failures(self):
        """A persistent broker outage must not mass-fail every QUEUED row on the session."""
        session_id = str(uuid.uuid4())
        queued_rows = [
            make_run(session_id=session_id, status=RunStatus.QUEUED)
            for _ in range(MAX_CONSECUTIVE_DISPATCH_FAILURES + 2)
        ]
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("queue down"))
            dispatch_next_queued(session_id)
        assert mock_task.aenqueue.call_count == MAX_CONSECUTIVE_DISPATCH_FAILURES

        for row in queued_rows:
            row.refresh_from_db()
        statuses = [row.status for row in queued_rows]
        assert statuses.count(RunStatus.FAILED) == MAX_CONSECUTIVE_DISPATCH_FAILURES
        assert statuses.count(RunStatus.QUEUED) == 2

    def test_backs_off_when_a_peer_holds_the_session(self):
        session_id = str(uuid.uuid4())
        make_run(session_id=session_id, status=RunStatus.RUNNING)
        queued = make_run(session_id=session_id, status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            dispatch_next_queued(session_id)

        mock_task.aenqueue.assert_not_called()
        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED


@pytest.mark.django_db(transaction=True)
class TestRunsThatWaitForAnother:
    @pytest.fixture
    def waiting(self):
        """A QUEUED run on its own session waiting for a RUNNING run on another."""
        awaited = make_run(session_id=str(uuid.uuid4()), status=RunStatus.RUNNING)
        return awaited, make_run(session_id=str(uuid.uuid4()), status=RunStatus.QUEUED, waits_for=awaited)

    @pytest.mark.parametrize("release", ["dispatch", "orphan-sweep"])
    def test_no_release_path_starts_it_while_the_awaited_run_is_unfinished(self, waiting, release):
        _, queued = waiting

        with patch("sessions.queue.run_job_task") as mock_task:
            if release == "dispatch":
                dispatch_next_queued(queued.session_id)
            else:
                release_orphan_queued_runs()

        mock_task.aenqueue.assert_not_called()
        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED

    def test_a_later_run_of_its_session_stays_behind_it(self, waiting):
        _, queued = waiting
        later = make_run(session_id=queued.session_id, status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            dispatch_next_queued(queued.session_id)

        mock_task.aenqueue.assert_not_called()
        later.refresh_from_db()
        assert later.status == RunStatus.QUEUED

    def test_it_starts_once_the_awaited_run_has_finished(self, waiting, create_db_task_result):
        awaited, queued = waiting
        Run.objects.filter(pk=awaited.pk).update(status=RunStatus.SUCCESSFUL)
        task = create_db_task_result(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            release_runs_waiting_for(awaited)

        queued.refresh_from_db()
        assert (queued.status, queued.task_result_id) == (RunStatus.READY, task.id)


@pytest.mark.django_db(transaction=True)
class TestReleaseOrphanQueuedRuns:
    def test_releases_the_oldest_queued_run_per_idle_session(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        oldest = make_run(session_id=session_id, status=RunStatus.QUEUED)
        make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            result = release_orphan_queued_runs()

        assert (result.released, result.skipped, result.errored) == (1, 1, 0)
        oldest.refresh_from_db()
        assert (oldest.status, oldest.task_result_id) == (RunStatus.READY, fake_task.id)

    def test_session_ids_narrow_the_sweep_to_those_sessions(self, create_db_task_result):
        wanted = make_run(session_id=str(uuid.uuid4()), status=RunStatus.QUEUED)
        other = make_run(session_id=str(uuid.uuid4()), status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=create_db_task_result().id))
            result = release_orphan_queued_runs(session_ids=Run.objects.filter(pk=wanted.pk).values("session_id"))

        assert result.released == 1
        wanted.refresh_from_db()
        other.refresh_from_db()
        assert (wanted.status, other.status) == (RunStatus.READY, RunStatus.QUEUED)

    def test_a_failed_link_counts_as_errored(self):
        orphan = make_run(session_id=str(uuid.uuid4()), status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
            result = release_orphan_queued_runs()

        assert (result.released, result.errored) == (0, 1)
        orphan.refresh_from_db()
        assert orphan.status == RunStatus.FAILED
        assert orphan.error_message.startswith(LINK_FAILED_PREFIX)
