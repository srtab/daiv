import uuid
from datetime import timedelta
from unittest.mock import patch

from django.tasks.base import DEFAULT_TASK_PRIORITY

import pytest
from django_tasks_db.models import DBTaskResult
from sessions.executor.lock import stale_cutoff
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin
from sessions.tasks import (
    drain_inboxes_cron_task,
    generate_batch_title_task,
    generate_title_task,
    resume_stalled_batches_cron_task,
    sync_stuck_runs_cron_task,
)
from sessions.titling import TitlerNotConfiguredError

from core.constants import TASK_QUEUE_INTERACTIVE
from tests.unit_tests.sessions.conftest import amake_owned_merge_request
from tests.unit_tests.sessions.inbox.conftest import make_event


def test_sync_stuck_runs_cron_task_dispatches_command():
    """The cron task dispatches the sync_stuck_runs management command.

    Guards the wiring (command name + the ``@locked_task`` decorator that ``.func()``
    exercises), not crontask/django.tasks framework behavior.
    """
    with patch("sessions.tasks.call_command") as mock_call_command:
        sync_stuck_runs_cron_task.func()

    mock_call_command.assert_called_once_with("sync_stuck_runs")


def test_resume_stalled_batches_cron_task_runs_the_sweep():
    with patch("sessions.delegation.resume_stalled_batches", return_value=0) as sweep:
        resume_stalled_batches_cron_task.func()

    sweep.assert_called_once_with()


@pytest.mark.django_db(transaction=True)
class TestDrainInboxesCronTask:
    def _session(self, **fields) -> Session:
        return Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", ref="main", **fields
        )

    def test_it_drains_only_free_or_stale_sessions_with_a_waking_event(self):
        free = self._session()
        stale = self._session(active_run_id="gone", last_active_at=stale_cutoff() - timedelta(minutes=1))
        busy = self._session(active_run_id="chat-run")
        quiet = self._session()
        for session in (free, stale, busy):
            make_event(session, EventKind.CI_FAILED)
        make_event(quiet, EventKind.NOTICE)

        with patch("sessions.inbox.drain.drain_logged") as drain:
            drain_inboxes_cron_task.func()

        assert sorted(call.args[0] for call in drain.call_args_list) == sorted([free.pk, stale.pk])

    def test_it_returns_events_of_a_run_whose_slot_went_stale(self):
        session = self._session()
        run = Run.objects.create(
            session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo", status=RunStatus.RUNNING
        )
        Session.objects.filter(pk=session.pk).update(
            active_run_id=str(run.pk), last_active_at=stale_cutoff() - timedelta(minutes=1)
        )
        event = make_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN)

        with patch("sessions.inbox.drain.drain_logged"):
            drain_inboxes_cron_task.func()

        event = InboxEvent.objects.get(pk=event.pk)
        assert (event.state, event.attempts, event.run_id) == (EventState.PENDING, 1, None)


@pytest.mark.django_db(transaction=True)
class TestClassifyPushTask:
    @pytest.fixture(autouse=True)
    def _no_drain(self):
        with patch("sessions.inbox.post.drain_logged"):
            yield

    async def _push(self, after: str) -> None:
        from sessions.tasks import classify_push_task

        await classify_push_task.func(
            repo_id="group/repo", branch="daiv/branch", before="e96f1f77", after=after, pusher_username="sfr",
            commit_count=2,
        )  # fmt: skip

    async def test_a_push_daiv_did_not_make_is_remembered_by_the_owning_session(self):
        ownership = await amake_owned_merge_request(last_daiv_sha="daiv-sha")

        await self._push("their-sha")

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.source_key, event.subject, event.ref) == (
            EventKind.BRANCH_PUSHED, False, "push:their-sha", "branch:daiv/branch", "daiv/branch",
        )  # fmt: skip
        assert event.payload == {
            "before": "e96f1f77", "after": "their-sha", "commit_count": 2, "pusher_username": "sfr",
        }  # fmt: skip

    async def test_the_commit_daiv_last_pushed_is_not_remembered(self):
        await amake_owned_merge_request(last_daiv_sha="daiv-sha")

        await self._push("daiv-sha")

        assert not await InboxEvent.objects.aexists()

    async def test_any_commit_a_daiv_run_pushed_is_not_remembered(self):
        ownership = await amake_owned_merge_request(last_daiv_sha="newer-daiv-sha")
        await Run.objects.acreate(
            session_id=ownership.session_id, trigger_type=SessionOrigin.CHAT, repo_id="group/repo",
            status=RunStatus.SUCCESSFUL, pushed_sha="older-daiv-sha",
        )  # fmt: skip

        await self._push("older-daiv-sha")

        assert not await InboxEvent.objects.aexists()

    async def test_a_push_to_a_branch_nobody_owns_is_ignored(self):
        await self._push("their-sha")

        assert not await InboxEvent.objects.aexists()


def _run_for_titling(
    *, title: str = "", session_title: str = "", batch_id: uuid.UUID | None = None, repo_id: str = "group/repo"
) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id=repo_id, title=session_title
    )
    return Run.objects.create(
        session=session, trigger_type=SessionOrigin.API_JOB, repo_id=repo_id, batch_id=batch_id, title=title
    )


def _chat_session(title: str = "Heuristic placeholder") -> Session:
    return Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/repo", title=title
    )


@pytest.mark.django_db
def test_generate_title_task_skips_a_missing_session_without_calling_the_model():
    with patch("sessions.titling.generate_title") as generate:
        generate_title_task.func(pk=str(uuid.uuid4()), prompt="any", repo_id="x/y")

    generate.assert_not_called()


@pytest.mark.django_db
def test_generate_title_task_overwrites_the_session_title_with_the_generated_one():
    session = _chat_session(title="add login")

    with patch("sessions.titling.generate_title", return_value="LLM generated") as generate:
        generate_title_task.func(pk=session.thread_id, prompt="add login", repo_id="group/repo", ref="feat/x")

    session.refresh_from_db()
    assert session.title == "LLM generated"
    generate.assert_called_once_with(
        "add login",
        repo_id="group/repo",
        ref="feat/x",
        run_metadata={
            "entity_type": "session",
            "entity_pk": session.thread_id,
            "repo_id": "group/repo",
            "ref": "feat/x",
        },
    )


@pytest.mark.django_db
def test_generate_title_task_keeps_a_title_renamed_before_it_ran():
    session = _chat_session(title="My rename")

    with patch("sessions.titling.generate_title", return_value="LLM generated"):
        generate_title_task.func(pk=session.thread_id, prompt="add login", repo_id="group/repo")

    session.refresh_from_db()
    assert session.title == "My rename"


@pytest.mark.django_db
def test_generate_title_task_keeps_a_title_renamed_while_the_model_answered():
    session = _chat_session(title="add login")

    def rename_then_answer(*args, **kwargs):
        Session.objects.filter(pk=session.pk).update(title="My rename")
        return "LLM generated"

    with patch("sessions.titling.generate_title", side_effect=rename_then_answer):
        generate_title_task.func(pk=session.thread_id, prompt="add login", repo_id="group/repo")

    session.refresh_from_db()
    assert session.title == "My rename"


@pytest.mark.django_db
def test_generate_title_task_keeps_the_title_when_no_model_is_configured():
    session = _chat_session()

    with patch("sessions.titling.generate_title", side_effect=TitlerNotConfiguredError("no key")):
        generate_title_task.func(pk=session.thread_id, prompt="any", repo_id="group/repo")

    session.refresh_from_db()
    assert session.title == "Heuristic placeholder"


@pytest.mark.django_db
def test_generate_title_task_lets_a_failed_model_call_fail_the_task():
    session = _chat_session()

    with (
        patch("sessions.titling.generate_title", side_effect=RuntimeError("provider down")),
        pytest.raises(RuntimeError, match="provider down"),
    ):
        generate_title_task.func(pk=session.thread_id, prompt="any", repo_id="group/repo")


@pytest.mark.django_db
def test_generate_batch_title_task_titles_only_the_untitled_runs_of_the_batch():
    batch_id = uuid.uuid4()
    untitled = [_run_for_titling(batch_id=batch_id, repo_id=f"o/r{i}") for i in range(2)]
    prefilled = _run_for_titling(batch_id=batch_id, title="job · run #1", repo_id="o/sched")
    outsider = _run_for_titling(repo_id="o/other")

    with patch("sessions.titling.generate_title", return_value="Add login feature") as generate:
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="add login")

    generate.assert_called_once_with("add login", run_metadata={"entity_type": "run_batch", "batch_id": str(batch_id)})
    titles = {run.pk: Run.objects.get(pk=run.pk).title for run in [*untitled, prefilled, outsider]}
    assert titles == {
        untitled[0].pk: "Add login feature",
        untitled[1].pk: "Add login feature",
        prefilled.pk: "job · run #1",
        outsider.pk: "",
    }


@pytest.mark.django_db
def test_generate_batch_title_task_backfills_only_untitled_sessions():
    batch_id = uuid.uuid4()
    untitled = _run_for_titling(batch_id=batch_id)
    pinned = _run_for_titling(batch_id=batch_id, session_title="Session pinned")

    with patch("sessions.titling.generate_title", return_value="Batch title"):
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

    assert Session.objects.get(pk=untitled.session_id).title == "Batch title"
    assert Session.objects.get(pk=pinned.session_id).title == "Session pinned"


@pytest.mark.django_db
def test_generate_batch_title_task_keeps_titles_when_no_model_is_configured():
    batch_id = uuid.uuid4()
    run = _run_for_titling(batch_id=batch_id)

    with patch("sessions.titling.generate_title", side_effect=TitlerNotConfiguredError("no key")):
        generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

    run.refresh_from_db()
    assert run.title == ""


@pytest.mark.django_db
def test_the_enqueued_title_row_is_what_the_interactive_worker_claims(database_task_backend):
    """DAIV's own backend overrides ``enqueue``, and the row is what a worker selects on."""
    result = generate_title_task.enqueue(pk=str(uuid.uuid4()), prompt="add login", repo_id="o/r")

    row = DBTaskResult.objects.get(id=result.id)
    assert row.queue_name == TASK_QUEUE_INTERACTIVE
    assert row.priority > DEFAULT_TASK_PRIORITY
    assert row.task_path == "sessions.tasks.generate_title_task"
