import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django_tasks.signals import task_finished, task_started
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signals import run_finished

from accounts.models import User


def _make_session(*, thread_id: str | None = None) -> Session:
    return Session.objects.create(
        thread_id=thread_id or str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="acme/api"
    )


def _create_run(*, session: Session, status: str = RunStatus.READY, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.API_JOB, "repo_id": "group/project", "status": status}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


def _make_run(*, session_id: str, status: str, **kwargs) -> Run:
    session = Session.objects.get_or_create(
        thread_id=session_id, defaults={"origin": SessionOrigin.API_JOB, "repo_id": "acme/api"}
    )[0]
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id="acme/api", status=status, prompt="p", **kwargs
    )


@pytest.mark.django_db
class TestBackfillSessionUser:
    def test_backfills_orphaned_runs_on_user_create(self):
        session = _make_session()
        orphan = _create_run(
            session=session,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            external_username="newdev",
            status=RunStatus.READY,
        )
        assert orphan.user is None

        user = User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user == user

    def test_backfills_orphaned_sessions_on_user_create(self):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()),
            origin=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            external_username="newdev2",
        )
        assert session.user is None

        user = User.objects.create_user(
            username="newdev2",
            email="newdev2@test.com",
            password="testpass",  # noqa: S106
        )

        session.refresh_from_db()
        assert session.user == user

    def test_does_not_backfill_already_linked_runs(self):
        existing_user = User.objects.create_user(
            username="existing",
            email="existing@test.com",
            password="testpass",  # noqa: S106
        )
        session = _make_session()
        linked = _create_run(
            session=session,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            user=existing_user,
            external_username="newdev",
            status=RunStatus.READY,
        )

        new_user = User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        linked.refresh_from_db()
        assert linked.user == existing_user, "Should not overwrite existing user FK"
        assert linked.user != new_user

    def test_does_not_backfill_on_user_update(self):
        session = _make_session()
        orphan = _create_run(
            session=session,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            external_username="devuser",
            status=RunStatus.READY,
        )

        user = User.objects.create_user(
            username="devuser",
            email="dev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user == user

        # Now unlink manually and update user — should NOT re-backfill
        Run.objects.filter(pk=orphan.pk).update(user=None)
        user.name = "Updated Name"
        user.save()

        orphan.refresh_from_db()
        assert orphan.user is None, "Should not backfill on user update, only on create"

    def test_no_match_when_external_username_differs(self):
        session = _make_session()
        orphan = _create_run(
            session=session,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="group/repo",
            external_username="other_user",
            status=RunStatus.READY,
        )

        User.objects.create_user(
            username="newdev",
            email="newdev@test.com",
            password="testpass",  # noqa: S106
        )

        orphan.refresh_from_db()
        assert orphan.user is None


@pytest.mark.django_db
class TestSyncRunOnTaskSignals:
    def test_task_finished_syncs_successful_run(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Job done.", "code_changes": True, "merge_request_id": 42},
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=finished,
        )
        session = _make_session()
        run = _create_run(session=session, task_result=tr, status=RunStatus.READY)

        task_finished.send(sender=type(None), task_result=tr.task_result)

        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL
        assert run.finished_at == finished
        assert run.result_summary == "Job done."
        assert run.code_changes is True
        assert run.merge_request_iid == 42

    def test_task_finished_syncs_failed_run(self, create_db_task_result):
        tr = create_db_task_result(
            status="FAILED",
            exception_class_path="builtins.ValueError",
            traceback="Traceback (most recent call last): ...",
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 4, 13, 11, 5, 0, tzinfo=UTC),
        )
        session = _make_session()
        run = _create_run(session=session, task_result=tr, status=RunStatus.RUNNING)

        task_finished.send(sender=type(None), task_result=tr.task_result)

        run.refresh_from_db()
        assert run.status == RunStatus.FAILED
        assert "ValueError" in run.error_message
        assert "Traceback" in run.error_message

    def test_task_started_syncs_running_status(self, create_db_task_result):
        started = datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(status="RUNNING", started_at=started)
        session = _make_session()
        run = _create_run(session=session, task_result=tr, status=RunStatus.READY)

        task_started.send(sender=type(None), task_result=tr.task_result)

        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert run.started_at == started

    def test_task_finished_no_run_does_not_raise(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "done"})
        task_finished.send(sender=type(None), task_result=tr.task_result)

    def test_task_started_no_run_does_not_raise(self, create_db_task_result):
        tr = create_db_task_result(status="RUNNING")
        task_started.send(sender=type(None), task_result=tr.task_result)

    def test_signal_handler_swallows_sync_errors(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "done"})
        session = _make_session()
        _create_run(session=session, task_result=tr, status=RunStatus.READY)

        with patch.object(Run, "sync_from_task_result", side_effect=RuntimeError("boom")):
            task_finished.send(sender=type(None), task_result=tr.task_result)


@pytest.mark.django_db
class TestRunFinishedSignal:
    def test_emitted_on_transition_to_successful(self, member_user):
        from unittest.mock import MagicMock

        from sessions.signals import emit_run_finished_if_terminal, run_finished

        session = _make_session()
        run = _create_run(session=session, status=RunStatus.RUNNING, user=member_user)
        received = MagicMock()
        run_finished.connect(received, dispatch_uid="test-succ")
        try:
            run.status = RunStatus.SUCCESSFUL
            run.save()
            emit_run_finished_if_terminal(run, previous_status=RunStatus.RUNNING)

            assert received.called
            _, kwargs = received.call_args
            assert kwargs["run"] is run
        finally:
            run_finished.disconnect(dispatch_uid="test-succ")

    def test_not_emitted_when_still_running(self, member_user):
        from unittest.mock import MagicMock

        from sessions.signals import emit_run_finished_if_terminal, run_finished

        session = _make_session()
        run = _create_run(session=session, status=RunStatus.RUNNING, user=member_user)
        received = MagicMock()
        run_finished.connect(received, dispatch_uid="test-run")
        try:
            emit_run_finished_if_terminal(run, previous_status=RunStatus.READY)
            assert not received.called
        finally:
            run_finished.disconnect(dispatch_uid="test-run")

    def test_not_emitted_when_already_terminal(self, member_user):
        from unittest.mock import MagicMock

        from sessions.signals import emit_run_finished_if_terminal, run_finished

        session = _make_session()
        run = _create_run(session=session, status=RunStatus.SUCCESSFUL, user=member_user)
        received = MagicMock()
        run_finished.connect(received, dispatch_uid="test-term")
        try:
            emit_run_finished_if_terminal(run, previous_status=RunStatus.SUCCESSFUL)
            assert not received.called
        finally:
            run_finished.disconnect(dispatch_uid="test-term")


@pytest.mark.django_db(transaction=True)
class TestDispatchNextInSession:
    def test_releases_oldest_queued_sibling(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        oldest = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        _make_run(session_id=session_id, status=RunStatus.QUEUED)  # newer sibling

        db_task = create_db_task_result()
        fake_task = MagicMock(id=db_task.id)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            run_finished.send(sender=Run, run=finished)

        oldest.refresh_from_db()
        assert oldest.status == RunStatus.READY
        assert oldest.task_result_id == fake_task.id

    def test_no_op_when_no_session_id(self):
        # Create a run without a session (shouldn't happen in practice but guard the code path)
        session = _make_session()
        _create_run(session=session, status=RunStatus.SUCCESSFUL)
        # Manually blank session_id via a mock
        finished_mock = MagicMock()
        finished_mock.session_id = None
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            run_finished.send(sender=Run, run=finished_mock)
            mock_task.aenqueue.assert_not_called()

    def test_no_op_when_no_queued_sibling(self):
        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            run_finished.send(sender=Run, run=finished)
            mock_task.aenqueue.assert_not_called()

    def test_dispatch_failure_marks_queued_failed_and_unblocks_chain(self, create_db_task_result):
        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        bad = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        next_q = _make_run(session_id=session_id, status=RunStatus.QUEUED)

        db_task = create_db_task_result()
        fake_task = MagicMock(id=db_task.id)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=[RuntimeError("queue down"), fake_task])
            run_finished.send(sender=Run, run=finished)

        bad.refresh_from_db()
        next_q.refresh_from_db()
        assert bad.status == RunStatus.FAILED
        assert "dispatch_failed" in (bad.error_message or "")
        assert next_q.status == RunStatus.READY
        assert next_q.task_result_id == fake_task.id

    def test_skip_dispatch_kwarg_suppresses_dispatcher(self):
        """run_finished with skip_dispatch=True must not re-enter dispatch_next_in_session."""
        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        queued = _make_run(session_id=session_id, status=RunStatus.QUEUED)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            run_finished.send(sender=Run, run=finished, skip_dispatch=True)
            mock_task.aenqueue.assert_not_called()
        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED

    def test_enqueue_failure_reemit_uses_skip_dispatch(self):
        """The dispatch-failure path must re-emit ``run_finished`` with
        ``skip_dispatch=True`` so the dispatcher does not recurse — notifications still fire."""
        from sessions.signals import _enqueue_queued_run

        session_id = str(uuid.uuid4())
        bad = _make_run(session_id=session_id, status=RunStatus.READY)
        captured: list = []

        def _spy(sender, run, **kwargs):
            captured.append(kwargs.get("skip_dispatch"))

        run_finished.connect(_spy, dispatch_uid="t-skip-test")
        try:
            with patch("sessions.signals.run_job_task") as mock_task:
                mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("queue down"))
                ok = _enqueue_queued_run(bad)
        finally:
            run_finished.disconnect(dispatch_uid="t-skip-test")

        assert ok is False
        assert captured == [True], f"expected one emit with skip_dispatch=True, got {captured}"

    def test_bails_after_max_consecutive_failures(self, create_db_task_result):
        """A persistent broker outage must not mass-fail every QUEUED row on the session."""
        from sessions.signals import MAX_CONSECUTIVE_DISPATCH_FAILURES

        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        queued_rows = [
            _make_run(session_id=session_id, status=RunStatus.QUEUED)
            for _ in range(MAX_CONSECUTIVE_DISPATCH_FAILURES + 2)
        ]
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(side_effect=RuntimeError("queue down"))
            run_finished.send(sender=Run, run=finished)
            assert mock_task.aenqueue.call_count == MAX_CONSECUTIVE_DISPATCH_FAILURES

        statuses = {RunStatus.FAILED: 0, RunStatus.QUEUED: 0}
        for row in queued_rows:
            row.refresh_from_db()
            statuses[row.status] = statuses.get(row.status, 0) + 1
        assert statuses[RunStatus.FAILED] == MAX_CONSECUTIVE_DISPATCH_FAILURES
        assert statuses[RunStatus.QUEUED] == 2

    def test_re_enqueue_propagates_agent_override(self, create_db_task_result):
        """Releasing a QUEUED sibling must forward the per-row agent override pair."""
        session_id = str(uuid.uuid4())
        finished = _make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        _make_run(
            session_id=session_id,
            status=RunStatus.QUEUED,
            agent_model="openrouter:anthropic/claude-opus-4.6",
            agent_thinking_level="high",
        )

        db_task = create_db_task_result()
        fake_task = MagicMock(id=db_task.id)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            run_finished.send(sender=Run, run=finished)

        kwargs = mock_task.aenqueue.call_args.kwargs
        assert kwargs["agent_model"] == "openrouter:anthropic/claude-opus-4.6"
        assert kwargs["agent_thinking_level"] == "high"
        assert "use_max" not in kwargs


@pytest.mark.django_db(transaction=True)
class TestSyncReleasesQueuedSibling:
    def test_terminal_dbtaskresult_releases_queued_sibling(self, create_db_task_result):
        """When sync_stuck_runs reconciles a stuck RUNNING Run whose
        DBTaskResult is already terminal, the resulting run_finished signal
        must release the oldest QUEUED sibling on the same session_id.
        """
        from django.core.management import call_command

        session_id = str(uuid.uuid4())
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "done"})
        stuck = _make_run(session_id=session_id, status=RunStatus.RUNNING, task_result=tr)
        queued = _make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            call_command("sync_stuck_runs")

        stuck.refresh_from_db()
        queued.refresh_from_db()
        assert stuck.status == RunStatus.SUCCESSFUL
        assert queued.status == RunStatus.READY
        assert queued.task_result_id == fake_task.id
