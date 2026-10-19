import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.models import Scope as SandboxScope
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import (
    LINK_FAILED_PREFIX,
    MAX_CONSECUTIVE_DISPATCH_FAILURES,
    aenqueue_run,
    amark_failed_and_advance,
    dispatch_next_queued,
    release_orphan_queued_runs,
)
from sessions.signal_defs import run_finished


def _make_run(*, session_id: str, status: str, trigger_type: str = SessionOrigin.API_JOB, **kwargs) -> Run:
    session = Session.objects.get_or_create(
        thread_id=session_id, defaults={"origin": trigger_type, "repo_id": "acme/api"}
    )[0]
    return Run.objects.create(
        session=session, trigger_type=trigger_type, repo_id="acme/api", status=status, prompt="p", **kwargs
    )


@pytest.fixture
def emitted():
    """The ``skip_dispatch`` value of every ``run_finished`` emit, by run id."""
    seen: list[tuple[str, bool | None]] = []

    def _spy(sender, run, **kwargs):
        seen.append((str(run.pk), kwargs.get("skip_dispatch")))

    run_finished.connect(_spy, dispatch_uid="test-queue-emitted")
    yield seen
    run_finished.disconnect(dispatch_uid="test-queue-emitted")


@pytest.mark.django_db(transaction=True)
class TestAenqueueRun:
    @pytest.mark.parametrize(
        ("trigger_type", "ask_user_enabled"),
        [(SessionOrigin.API_JOB, True), (SessionOrigin.SCHEDULE, False), (SessionOrigin.PIPELINE_WEBHOOK, False)],
    )
    async def test_the_task_kwargs_come_from_the_row(
        self, create_db_task_result, member_user, trigger_type, ask_user_enabled
    ):
        env = await SandboxEnvironment.objects.acreate(scope=SandboxScope.GLOBAL, name="ci", base_image="python:3.14")
        run = await sync_to_async(_make_run)(
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
            assert await aenqueue_run(run, log_context="test") is True

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
        }
        await run.arefresh_from_db()
        assert run.task_result_id == task.id

    async def test_blank_row_fields_reach_the_task_as_none(self, create_db_task_result):
        run = await sync_to_async(_make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)
        task = await sync_to_async(create_db_task_result)(status="READY")

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task.id))
            await aenqueue_run(run, log_context="test")

        kwargs = mock_task.aenqueue.call_args.kwargs
        assert (kwargs["ref"], kwargs["agent_model"], kwargs["agent_thinking_level"]) == (None, None, None)
        assert (kwargs["sandbox_environment_id"], kwargs["user_id"]) == (None, None)

    @pytest.mark.parametrize("skip_dispatch", [False, True])
    async def test_a_failed_enqueue_fails_the_row_and_emits(self, emitted, skip_dispatch):
        run = await sync_to_async(_make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("queue down"))
            assert await aenqueue_run(run, log_context="test", skip_dispatch=skip_dispatch) is False

        await run.arefresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.error_message == "enqueue_failed: RuntimeError: queue down"
        assert run.finished_at is not None
        assert run.task_result_id is None
        assert emitted == [(str(run.pk), skip_dispatch)]

    async def test_a_failed_link_fails_the_row_with_the_link_prefix_and_emits(self, emitted):
        run = await sync_to_async(_make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))  # no such task result row
            assert await aenqueue_run(run, log_context="test") is False

        await run.arefresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.error_message.startswith(f"{LINK_FAILED_PREFIX}: IntegrityError")
        assert run.task_result_id is None
        assert emitted == [(str(run.pk), False)]


@pytest.mark.django_db(transaction=True)
class TestAmarkFailedAndAdvance:
    async def test_a_row_that_got_running_meanwhile_keeps_its_status(self, emitted):
        """A link that raised after it committed lets the worker move the row on; failing it then would hide the
        result of a run that is going ahead."""
        run = await sync_to_async(_make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.RUNNING)

        await amark_failed_and_advance(run, prefix=LINK_FAILED_PREFIX, err=RuntimeError("x"), log_context="test")

        assert run.error_message == f"{LINK_FAILED_PREFIX}: RuntimeError: x"
        await run.arefresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.RUNNING, "")
        assert emitted == []

    async def test_a_failed_write_is_logged_without_emitting(self, emitted, caplog):
        run = await sync_to_async(_make_run)(session_id=str(uuid.uuid4()), status=RunStatus.READY)

        with patch.object(Run.objects, "afinish", AsyncMock(side_effect=RuntimeError("db down"))):
            await amark_failed_and_advance(run, prefix="enqueue_failed", err=RuntimeError("x"), log_context="test")

        assert f"test: failed to mark run={run.pk} FAILED" in caplog.text
        assert emitted == []


@pytest.mark.django_db(transaction=True)
class TestDispatchNextQueued:
    def test_releases_the_oldest_queued_sibling(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        oldest = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        newer = _make_run(session_id=session_id, status=RunStatus.QUEUED)

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
        _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            dispatch_next_queued(session_id)
        mock_task.aenqueue.assert_not_called()

    def test_a_failed_enqueue_fails_the_row_and_moves_to_the_next(self, create_db_task_result, emitted):
        session_id = str(uuid.uuid4())
        bad = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        next_q = _make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=[RuntimeError("queue down"), fake_task])
            dispatch_next_queued(session_id)

        bad.refresh_from_db()
        next_q.refresh_from_db()
        assert bad.status == RunStatus.FAILED
        assert bad.error_message == "enqueue_failed: RuntimeError: queue down"
        assert (next_q.status, next_q.task_result_id) == (RunStatus.READY, fake_task.id)
        # The re-emit must not re-enter the dispatcher, which moves on by itself.
        assert emitted == [(str(bad.pk), True)]

    def test_a_failed_link_fails_the_row_and_moves_to_the_next(self, create_db_task_result, emitted):
        session_id = str(uuid.uuid4())
        unlinked = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        next_q = _make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=[MagicMock(id=uuid.uuid4()), fake_task])
            dispatch_next_queued(session_id)

        unlinked.refresh_from_db()
        next_q.refresh_from_db()
        assert unlinked.status == RunStatus.FAILED
        assert unlinked.error_message.startswith(LINK_FAILED_PREFIX)
        assert (next_q.status, next_q.task_result_id) == (RunStatus.READY, fake_task.id)
        assert emitted == [(str(unlinked.pk), True)]

    def test_bails_after_max_consecutive_failures(self):
        """A persistent broker outage must not mass-fail every QUEUED row on the session."""
        session_id = str(uuid.uuid4())
        queued_rows = [
            _make_run(session_id=session_id, status=RunStatus.QUEUED)
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
        _make_run(session_id=session_id, status=RunStatus.RUNNING)
        queued = _make_run(session_id=session_id, status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            dispatch_next_queued(session_id)

        mock_task.aenqueue.assert_not_called()
        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED


@pytest.mark.django_db(transaction=True)
class TestReleaseOrphanQueuedRuns:
    def test_releases_the_oldest_queued_run_per_idle_session(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        oldest = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        _make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            result = release_orphan_queued_runs()

        assert (result.released, result.skipped, result.errored) == (1, 1, 0)
        oldest.refresh_from_db()
        assert (oldest.status, oldest.task_result_id) == (RunStatus.READY, fake_task.id)

    def test_a_failed_link_counts_as_errored(self):
        orphan = _make_run(session_id=str(uuid.uuid4()), status=RunStatus.QUEUED)

        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
            result = release_orphan_queued_runs()

        assert (result.released, result.errored) == (0, 1)
        orphan.refresh_from_db()
        assert orphan.status == RunStatus.FAILED
        assert orphan.error_message.startswith(LINK_FAILED_PREFIX)
