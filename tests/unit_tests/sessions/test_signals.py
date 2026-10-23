import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from django.tasks import task
from django.tasks.signals import task_finished, task_started

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signal_defs import run_finished

from accounts.models import User
from codebase.models import CrossProjectAccessRecord
from core.constants import CrossProjectOutcome
from tests.unit_tests.sessions.conftest import commit_revision, make_artifact, make_run


@task
def noop_task() -> None:
    return None


def _make_session(*, thread_id: str | None = None) -> Session:
    return Session.objects.create(
        thread_id=thread_id or str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="acme/api"
    )


def _create_run(*, session: Session, status: str = RunStatus.READY, **kwargs) -> Run:
    defaults = {"trigger_type": SessionOrigin.API_JOB, "repo_id": "group/project", "status": status}
    defaults.update(kwargs)
    return Run.objects.create(session=session, **defaults)


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

    def test_non_database_task_result_is_ignored(self, caplog):
        noop_task.enqueue()

        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


@pytest.mark.django_db(transaction=True)
class TestDispatchNextInSession:
    def test_dispatches_the_finished_runs_session(self):
        finished = make_run(session_id=str(uuid.uuid4()), status=RunStatus.SUCCESSFUL)

        with patch("sessions.signals.dispatch_next_queued") as dispatch:
            run_finished.send(sender=Run, run=finished)

        dispatch.assert_called_once_with(finished.session_id)

    def test_no_op_when_no_session_id(self):
        # Create a run without a session (shouldn't happen in practice but guard the code path)
        session = _make_session()
        _create_run(session=session, status=RunStatus.SUCCESSFUL)
        # Manually blank session_id via a mock
        finished_mock = MagicMock()
        finished_mock.session_id = None
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            run_finished.send(sender=Run, run=finished_mock)
            mock_task.aenqueue.assert_not_called()

    def test_skip_dispatch_kwarg_suppresses_dispatcher(self):
        """run_finished with skip_dispatch=True must not re-enter dispatch_next_in_session."""
        session_id = str(uuid.uuid4())
        finished = make_run(session_id=session_id, status=RunStatus.SUCCESSFUL)
        queued = make_run(session_id=session_id, status=RunStatus.QUEUED)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            run_finished.send(sender=Run, run=finished, skip_dispatch=True)
            mock_task.aenqueue.assert_not_called()
        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED


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
        stuck = make_run(session_id=session_id, status=RunStatus.RUNNING, task_result=tr)
        queued = make_run(session_id=session_id, status=RunStatus.QUEUED)

        fake_task = MagicMock(id=create_db_task_result().id)
        with patch("sessions.queue.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            call_command("sync_stuck_runs")

        stuck.refresh_from_db()
        queued.refresh_from_db()
        assert stuck.status == RunStatus.SUCCESSFUL
        assert queued.status == RunStatus.READY
        assert queued.task_result_id == fake_task.id


@pytest.mark.django_db
def test_deleting_run_removes_artifact_rows_and_stored_files_on_commit(django_capture_on_commit_callbacks):
    from django.core.files.storage import default_storage

    from sessions.models import RunArtifact

    run = _create_run(session=_make_session(), status=RunStatus.SUCCESSFUL)
    artifact = make_artifact(run)
    name = artifact.file.name

    with django_capture_on_commit_callbacks() as callbacks:
        run.delete()
        assert default_storage.exists(name)

    assert len(callbacks) == 1
    callbacks[0]()
    assert not RunArtifact.objects.filter(pk=artifact.pk).exists()
    assert not default_storage.exists(name)


@pytest.mark.django_db
def test_deleting_an_artifact_loaded_before_a_revision_removes_the_revised_file(django_capture_on_commit_callbacks):
    from django.core.files.storage import default_storage

    run = _create_run(session=_make_session(), status=RunStatus.SUCCESSFUL)
    stale = make_artifact(run)
    neighbour = make_artifact(run, filename="other.md")
    revised = commit_revision(stale, b"# v2")

    with django_capture_on_commit_callbacks(execute=True):
        stale.delete()

    assert not default_storage.exists(revised)
    assert default_storage.exists(neighbour.file.name)


@pytest.mark.django_db
def test_artifact_file_delete_failure_is_logged(django_capture_on_commit_callbacks, caplog):
    from sessions.models import RunArtifact

    artifact = make_artifact(_create_run(session=_make_session(), status=RunStatus.SUCCESSFUL))

    with (
        patch.object(artifact.file.storage, "delete", side_effect=OSError("disk gone")),
        django_capture_on_commit_callbacks(execute=True),
    ):
        artifact.delete()

    assert not RunArtifact.objects.filter(pk=artifact.pk).exists()
    assert "Failed to delete artifact file" in caplog.text


@pytest.mark.django_db
class TestRestrictSessionToCrossProjectFetchers:
    @staticmethod
    def _record(*, thread_id: str, outcome=CrossProjectOutcome.ALLOWED, acting_user=None) -> CrossProjectAccessRecord:
        return CrossProjectAccessRecord.objects.create(
            thread_id=thread_id,
            acting_user=acting_user,
            provider="gitlab",
            target_repo_id="other/repo",
            outcome=outcome,
        )

    def test_an_allowed_fetch_adds_the_person_once(self, member_user):
        session = _make_session()

        self._record(thread_id=session.pk, acting_user=member_user)
        self._record(thread_id=session.pk, acting_user=member_user)

        session.refresh_from_db()
        assert session.cross_project_user_ids == [member_user.pk]

    def test_a_second_person_is_appended(self, member_user, other_user):
        session = _make_session()

        self._record(thread_id=session.pk, acting_user=member_user)
        self._record(thread_id=session.pk, acting_user=other_user)

        session.refresh_from_db()
        assert session.cross_project_user_ids == [member_user.pk, other_user.pk]

    @pytest.mark.parametrize(
        "outcome", [outcome for outcome in CrossProjectOutcome if outcome != CrossProjectOutcome.ALLOWED]
    )
    def test_a_refused_attempt_restricts_nothing(self, member_user, outcome):
        session = _make_session()

        self._record(thread_id=session.pk, acting_user=member_user, outcome=outcome)

        session.refresh_from_db()
        assert session.cross_project_user_ids == []

    def test_an_unattributed_fetch_restricts_the_session_to_admins_and_logs_an_error(self, caplog):
        session = _make_session()

        self._record(thread_id=session.pk)

        session.refresh_from_db()
        assert session.cross_project_user_ids == [None]
        assert any(record.levelname == "ERROR" for record in caplog.records)

    def test_a_fetch_in_a_thread_without_a_session_row_raises_and_logs_an_error(self, member_user, caplog):
        thread_id = str(uuid.uuid4())

        with pytest.raises(Session.DoesNotExist):
            self._record(thread_id=thread_id, acting_user=member_user)

        errors = [record for record in caplog.records if record.levelname == "ERROR"]
        assert [record.args for record in errors] == [(thread_id,)]
        assert not Session.objects.exclude(cross_project_user_ids=[]).exists()

    def test_an_updated_record_is_not_counted_again(self, member_user, other_user):
        session = _make_session()
        record = self._record(thread_id=session.pk, acting_user=member_user)
        Session.objects.filter(pk=session.pk).update(cross_project_user_ids=[])

        record.acting_user = other_user
        record.save()

        session.refresh_from_db()
        assert session.cross_project_user_ids == []
