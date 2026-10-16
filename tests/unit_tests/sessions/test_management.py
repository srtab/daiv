import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from sessions.models import Run, RunStatus, Session, SessionArchive, SessionOrigin

from tests.unit_tests.sessions.conftest import archive_fields


def _make_session(*, thread_id: str | None = None) -> Session:
    return Session.objects.create(
        thread_id=thread_id or str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="group/project"
    )


@pytest.mark.django_db(transaction=True)
class TestSyncStuckRunsCommand:
    def test_syncs_stuck_running_run(self, caplog, create_db_task_result):
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

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL
        assert run.finished_at == finished
        assert run.result_summary == "Done."
        assert any("Synced: 1" in rec.message and rec.levelname == "INFO" for rec in caplog.records)

    def test_skips_terminal_runs(self, caplog, create_db_task_result):
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

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        assert any("Synced: 0" in rec.message for rec in caplog.records)

    def test_counts_already_synced_run_as_skipped(self, caplog, create_db_task_result):
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

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        assert any("Synced: 0, already up to date: 1" in rec.message for rec in caplog.records)

    def test_skips_runs_without_task_result(self, caplog):
        session = _make_session()
        Run.objects.create(
            session=session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, repo_id="group/project", status=RunStatus.RUNNING
        )

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        assert any("Synced: 0" in rec.message for rec in caplog.records)

    def test_reaps_orphaned_chat_run_when_session_heartbeat_stale(self, caplog):
        """A task-less chat run stuck RUNNING is failed once its session heartbeat goes stale."""
        stale = timezone.now() - timedelta(hours=1)
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", last_active_at=stale
        )
        run = Run.objects.create(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id="group/project", status=RunStatus.RUNNING
        )

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        run.refresh_from_db()
        assert run.status == RunStatus.FAILED
        assert run.finished_at is not None
        assert run.error_message
        assert any("chat runs reaped: 1" in rec.message for rec in caplog.records)

    def test_does_not_reap_live_chat_run(self, caplog):
        """A chat run whose session is still heartbeating (fresh last_active_at) is left alone."""
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project"
        )
        run = Run.objects.create(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id="group/project", status=RunStatus.RUNNING
        )

        with caplog.at_level("INFO", logger="daiv.sessions"):
            call_command("sync_stuck_runs")

        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert any("chat runs reaped: 0" in rec.message for rec in caplog.records)

    def test_continues_after_per_row_error(self, caplog, create_db_task_result):
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

        with (
            patch.object(Run, "sync_and_save", selectively_raise),
            caplog.at_level("WARNING", logger="daiv.sessions"),
            pytest.raises(CommandError) as exc_info,
        ):
            call_command("sync_stuck_runs")

        ok_run.refresh_from_db()
        bad_run.refresh_from_db()
        assert ok_run.status == RunStatus.SUCCESSFUL
        assert bad_run.status == RunStatus.RUNNING
        assert "Synced: 1" in str(exc_info.value)
        assert "errored: 1" in str(exc_info.value)
        # The failed run id is included so the failed task record is self-contained.
        assert str(bad_run.pk) in str(exc_info.value)
        # The error summary is logged at WARNING (the level is the monitoring contract).
        assert any("errored: 1" in rec.message and rec.levelname == "WARNING" for rec in caplog.records)


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


@pytest.mark.django_db(transaction=True)
class TestArchiveSessionsCommand:
    def test_it_archives_live_threads_and_skips_sessions_already_archived(self):
        live, expired, archived = (_make_session() for _ in range(3))
        SessionArchive.objects.create(session=archived, **archive_fields(0, [HumanMessage(content="old", id="o1")]))
        checkpoints = {
            live.thread_id: SimpleNamespace(checkpoint={"channel_values": {"messages": [HumanMessage("hi", id="h1")]}})
        }
        checkpointer = SimpleNamespace(
            aget_tuple=AsyncMock(side_effect=lambda config: checkpoints.get(config["configurable"]["thread_id"]))
        )

        @asynccontextmanager
        async def _open_checkpointer():
            yield checkpointer

        out = StringIO()
        with patch("core.checkpointer.open_checkpointer", _open_checkpointer):
            call_command("archive_sessions", stdout=out)

        assert "Archived 1 session(s); 1 had nothing left to archive." in out.getvalue()
        assert list(SessionArchive.objects.filter(session=live).values_list("first_message_id", flat=True)) == ["h1"]
        assert not SessionArchive.objects.filter(session=expired).exists()
        read = [call.args[0]["configurable"]["thread_id"] for call in checkpointer.aget_tuple.await_args_list]
        assert archived.thread_id not in read

    def test_it_fails_naming_the_sessions_it_could_not_archive(self):
        failing, live = _make_session(), _make_session()
        checkpoint = SimpleNamespace(checkpoint={"channel_values": {"messages": [HumanMessage("hi", id="h1")]}})

        def _read(config):
            if config["configurable"]["thread_id"] == failing.thread_id:
                raise ConnectionError("redis down")
            return checkpoint

        @asynccontextmanager
        async def _open_checkpointer():
            yield SimpleNamespace(aget_tuple=AsyncMock(side_effect=_read))

        out = StringIO()
        with (
            patch("core.checkpointer.open_checkpointer", _open_checkpointer),
            pytest.raises(CommandError, match=f"Failed to archive 1 session.*{failing.thread_id}"),
        ):
            call_command("archive_sessions", stdout=out)

        assert "Archived 1 session(s)" in out.getvalue()
        assert SessionArchive.objects.filter(session=live).exists()


@pytest.mark.django_db
class TestExportSessionArchivesCommand:
    def test_it_writes_each_archived_session_with_its_contexts_and_runs(self, tmp_path):
        session = _make_session()
        SessionArchive.objects.create(
            session=session, **archive_fields(0, [HumanMessage("fix the bug", id="h1")], closed=True)
        )
        SessionArchive.objects.create(session=session, **archive_fields(1, [AIMessage("cleared", id="r1")]))
        Run.objects.create(
            session=session, trigger_type=SessionOrigin.API_JOB, repo_id=session.repo_id, status=RunStatus.SUCCESSFUL
        )
        _make_session()

        call_command("export_session_archives", str(tmp_path), stdout=StringIO())

        [exported] = tmp_path.iterdir()
        payload = json.loads(exported.read_text())
        assert exported.name == f"{session.thread_id}.json"
        assert [context["epoch"] for context in payload["contexts"]] == [0, 1]
        assert [context["closed_at"] is not None for context in payload["contexts"]] == [True, False]
        assert payload["contexts"][0]["messages"][0]["data"]["content"] == "fix the bug"
        assert [run["status"] for run in payload["runs"]] == [RunStatus.SUCCESSFUL]

    def test_it_exports_only_sessions_active_since_the_given_day(self, tmp_path):
        recent, stale = _make_session(), _make_session()
        Session.objects.filter(pk=stale.pk).update(last_active_at=datetime(2026, 9, 20, 12, tzinfo=UTC))
        Session.objects.filter(pk=recent.pk).update(last_active_at=datetime(2026, 10, 1, 12, tzinfo=UTC))
        for session in (recent, stale):
            SessionArchive.objects.create(session=session, **archive_fields(0, [HumanMessage("hi", id="h1")]))

        call_command("export_session_archives", str(tmp_path), "--since", "2026-10-01", stdout=StringIO())

        assert [path.name for path in tmp_path.iterdir()] == [f"{recent.thread_id}.json"]

    def test_a_session_that_fails_to_export_is_named_and_the_rest_written(self, tmp_path):
        broken, kept = _make_session(), _make_session()
        SessionArchive.objects.create(
            session=broken, **{**archive_fields(0, [HumanMessage("hi", id="h1")]), "state": b"not zlib"}
        )
        SessionArchive.objects.create(session=kept, **archive_fields(0, [HumanMessage("hi", id="h1")]))

        with pytest.raises(CommandError, match=f"Failed to export 1 session.*{broken.thread_id}"):
            call_command("export_session_archives", str(tmp_path), stdout=StringIO())

        assert [path.name for path in tmp_path.iterdir()] == [f"{kept.thread_id}.json"]

    def test_it_exports_only_the_requested_repository(self, tmp_path):
        kept = _make_session()
        other = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="other/repo")
        for session in (kept, other):
            SessionArchive.objects.create(session=session, **archive_fields(0, [HumanMessage("hi", id="h1")]))

        call_command("export_session_archives", str(tmp_path), "--repo", "group/project", stdout=StringIO())

        assert [path.name for path in tmp_path.iterdir()] == [f"{kept.thread_id}.json"]
