import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin


def _make_session(*, thread_id: str | None = None) -> Session:
    return Session.objects.create(
        thread_id=thread_id or str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="group/project"
    )


@pytest.mark.django_db(transaction=True)
class TestSyncStuckRunsCommand:
    def test_syncs_stuck_running_run(self, create_db_task_result):
        finished = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Done.", "code_changes": False},
            started_at=datetime(2026, 4, 13, 11, 0, 0, tzinfo=UTC),
            finished_at=finished,
        )
        session = _make_session()
        run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.RUNNING,
            task_result=tr,
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL
        assert run.finished_at == finished
        assert run.result_summary == "Done."
        assert "Synced: 1" in out.getvalue()

    def test_skips_terminal_runs(self, create_db_task_result):
        tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "Already done."})
        session = _make_session()
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.SUCCESSFUL,
            task_result=tr,
            result_summary="Already done.",
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        assert "Synced: 0" in out.getvalue()

    def test_counts_already_synced_run_as_skipped(self, create_db_task_result):
        """A non-terminal Run already in sync with its DBTaskResult counts toward `skipped`."""
        tr = create_db_task_result(status="READY")
        session = _make_session()
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.READY,
            task_result=tr,
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        assert "Synced: 0, already up to date: 1" in out.getvalue()

    def test_skips_runs_without_task_result(self):
        session = _make_session()
        Run.objects.create(
            session=session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/project", status=RunStatus.RUNNING
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        assert "Synced: 0" in out.getvalue()

    def test_reaps_orphaned_chat_run_when_session_heartbeat_stale(self):
        """A task-less chat run stuck RUNNING is failed once its session heartbeat goes stale."""
        stale = timezone.now() - timedelta(hours=1)
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", last_active_at=stale
        )
        run = Run.objects.create(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id="group/project", status=RunStatus.RUNNING
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        run.refresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.finished_at is not None
        assert run.error_message
        assert "chat runs reaped: 1" in out.getvalue()

    def test_does_not_reap_live_chat_run(self):
        """A chat run whose session is still heartbeating (fresh last_active_at) is left alone."""
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project"
        )
        run = Run.objects.create(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id="group/project", status=RunStatus.RUNNING
        )

        out = StringIO()
        call_command("sync_stuck_runs", stdout=out)

        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert "chat runs reaped: 0" in out.getvalue()

    def test_continues_after_per_row_error(self, create_db_task_result):
        ok_tr = create_db_task_result(
            status="SUCCESSFUL",
            return_value={"response": "Done."},
            finished_at=datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC),
        )
        bad_tr = create_db_task_result(status="SUCCESSFUL", return_value={"response": "boom."})

        session = _make_session()
        ok_run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.RUNNING,
            task_result=ok_tr,
        )
        bad_session = _make_session()
        bad_run = Run.objects.create(
            session=bad_session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.RUNNING,
            task_result=bad_tr,
        )

        original = Run.sync_and_save

        def selectively_raise(self):
            if self.pk == bad_run.pk:
                raise RuntimeError("simulated sync failure")
            return original(self)

        out = StringIO()
        with patch.object(Run, "sync_and_save", selectively_raise), pytest.raises(CommandError) as exc_info:
            call_command("sync_stuck_runs", stdout=out)

        ok_run.refresh_from_db()
        bad_run.refresh_from_db()
        assert ok_run.status == RunStatus.SUCCESSFUL
        assert bad_run.status == RunStatus.RUNNING
        assert "Synced: 1" in str(exc_info.value)
        assert "errored: 1" in str(exc_info.value)


@pytest.mark.django_db(transaction=True)
class TestReleaseOrphanQueuedSessionsCommand:
    def test_releases_queued_when_no_active_sibling(self, create_db_task_result):
        """A QUEUED run with no READY/RUNNING sibling on the session is dispatched."""
        session = _make_session()
        orphan = Run.objects.create(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", status=RunStatus.QUEUED, prompt="p"
        )
        fake_task = MagicMock(id=create_db_task_result().id)
        out = StringIO()
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=fake_task)
            call_command("release_orphan_queued_sessions", stdout=out)

        orphan.refresh_from_db()
        assert orphan.status == RunStatus.READY
        assert orphan.task_result_id == fake_task.id
        assert "Released: 1" in out.getvalue()

    def test_skips_queued_when_active_sibling_exists(self):
        """A QUEUED run whose session already has a READY/RUNNING sibling is left alone."""
        session = _make_session()
        Run.objects.create(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", status=RunStatus.RUNNING, prompt="p"
        )
        queued = Run.objects.create(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", status=RunStatus.QUEUED, prompt="p"
        )
        out = StringIO()
        with patch("sessions.signals.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            call_command("release_orphan_queued_sessions", stdout=out)
            mock_task.aenqueue.assert_not_called()

        queued.refresh_from_db()
        assert queued.status == RunStatus.QUEUED
        assert "Released: 0" in out.getvalue()
