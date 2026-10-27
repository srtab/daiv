import uuid
from unittest.mock import MagicMock, patch

from django.db.models import Q
from django.test import TestCase

import pytest
from asgiref.sync import sync_to_async
from django_tasks_db.models import DBTaskResult
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signal_defs import run_ended, run_finished

from core.constants import CANCELLED_BY_COORDINATOR_MESSAGE
from tests.unit_tests.sessions.conftest import make_task_result


def _create_run(*, status: str = RunStatus.RUNNING, trigger_type: str = SessionOrigin.PIPELINE_WEBHOOK) -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="acme/api")
    return Run.objects.create(session=session, trigger_type=trigger_type, repo_id="acme/api", status=status)


@pytest.mark.django_db
class TestFinish:
    def test_it_writes_the_status_and_fields_and_updates_the_instance(self, run_finished_spy, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="boom") is True

        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        badge.assert_called_once()
        assert run_finished_spy.call_args.kwargs["run"] is run
        assert run_finished_spy.call_args.kwargs["skip_dispatch"] is False
        assert run_finished_spy.call_args.kwargs["never_started"] is False

    def test_the_badge_refresh_waits_for_the_commit(self, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            Run.objects.finish(run, RunStatus.SUCCESSFUL)
        badge.assert_not_called()
        for callback in callbacks:
            callback()
        badge.assert_called_once()

    def test_it_forwards_the_emit_flags(self, run_finished_spy):
        run = _create_run()

        Run.objects.finish(run, RunStatus.FAILED, skip_dispatch=True, never_started=True)

        assert run_finished_spy.call_args.kwargs["skip_dispatch"] is True
        assert run_finished_spy.call_args.kwargs["never_started"] is True

    def test_a_chat_run_refreshes_the_badge_without_emitting(self, run_finished_spy, badge):
        run = _create_run(trigger_type=SessionOrigin.CHAT)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True

        badge.assert_called_once()
        assert not run_finished_spy.called

    @pytest.mark.parametrize("trigger_type", [SessionOrigin.CHAT, SessionOrigin.API_JOB])
    def test_every_finished_run_emits_run_ended_chat_included(self, trigger_type):
        run = _create_run(trigger_type=trigger_type)
        spy = MagicMock()
        run_ended.connect(spy, dispatch_uid="tests-run-ended-spy")
        try:
            Run.objects.finish(run, RunStatus.SUCCESSFUL)
        finally:
            run_ended.disconnect(dispatch_uid="tests-run-ended-spy")

        assert spy.call_args.kwargs["run"] is run

    def test_a_finished_row_is_never_overwritten(self, run_finished_spy, badge):
        run = _create_run()
        Run.objects.filter(pk=run.pk).update(status=RunStatus.SUCCESSFUL)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="late") is False

        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.SUCCESSFUL, "")
        badge.assert_not_called()
        assert not run_finished_spy.called

    def test_a_row_failing_the_guard_is_left_alone(self, run_finished_spy):
        run = _create_run(status=RunStatus.RUNNING)

        assert Run.objects.finish(run, RunStatus.FAILED, where=Q(status=RunStatus.READY)) is False

        assert run.status == RunStatus.RUNNING
        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert not run_finished_spy.called

    def test_a_non_terminal_status_is_refused(self):
        run = _create_run(status=RunStatus.READY)

        with pytest.raises(ValueError, match="not a terminal run status"):
            Run.objects.finish(run, RunStatus.RUNNING)

    def test_a_failing_receiver_neither_stops_the_others_nor_the_finish(self):
        def _broken(sender, run, **kwargs):
            raise RuntimeError("receiver down")

        later = MagicMock()
        run_finished.connect(_broken, dispatch_uid="test-managers-broken")
        run_finished.connect(later, dispatch_uid="test-managers-later")
        try:
            run = _create_run()
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True
        finally:
            run_finished.disconnect(dispatch_uid="test-managers-broken")
            run_finished.disconnect(dispatch_uid="test-managers-later")

        later.assert_called_once()
        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL


@pytest.mark.django_db
class TestCancel:
    @pytest.mark.parametrize("status", [RunStatus.READY, RunStatus.QUEUED])
    def test_a_run_that_is_not_running_finishes_failed_at_once(self, run_finished_spy, badge, status):
        run = _create_run(status=status)

        assert Run.objects.cancel(run) is True

        assert run.status == RunStatus.FAILED
        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, CANCELLED_BY_COORDINATOR_MESSAGE)
        assert run.started_at is not None and run.finished_at is not None
        assert run.is_cancelled
        assert run_finished_spy.call_args.kwargs["never_started"] is True

    def test_its_queued_task_fails_with_it_so_no_worker_claims_it(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.READY)
        Run.objects.filter(pk=run.pk).update(task_result=make_task_result(status="READY"))

        assert Run.objects.cancel(run) is True

        task_result = DBTaskResult.objects.get(run=run)
        assert task_result.status == "FAILED"
        assert task_result.exception_class_path == "sessions.executor.run.RunStoppedError"

    def test_a_task_a_worker_claimed_is_left_to_it(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.READY)
        Run.objects.filter(pk=run.pk).update(task_result=make_task_result(status="RUNNING"))

        assert Run.objects.cancel(run) is True

        assert DBTaskResult.objects.get(run=run).status == "RUNNING"
        run.refresh_from_db()
        assert run.is_cancelled

    def test_a_running_run_is_only_asked_to_stop(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.RUNNING)

        assert Run.objects.cancel(run) is True

        assert run.status == RunStatus.RUNNING
        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.RUNNING, "")
        assert run.cancel_requested_at is not None
        assert not run.is_cancelled
        assert not run_finished_spy.called

    def test_a_running_run_keeps_what_its_task_recorded(self, run_finished_spy, badge):
        task_result = make_task_result(
            status="SUCCESSFUL",
            return_value={
                "response": "Opened the merge request.",
                "code_changes": True,
                "merge_request_id": 7,
                "merge_request_web_url": "https://git.example/mr/7",
            },
        )
        run = _create_run(status=RunStatus.RUNNING)
        Run.objects.filter(pk=run.pk).update(task_result=task_result)
        Run.objects.cancel(run)

        run = Run.objects.select_related("task_result").get(pk=run.pk)
        assert run.sync_and_save() is True

        run.refresh_from_db()
        assert (run.status, run.merge_request_web_url) == (RunStatus.SUCCESSFUL, "https://git.example/mr/7")
        assert not run.is_cancelled

    def test_a_running_run_its_task_stops_is_cancelled(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.RUNNING)
        Run.objects.filter(pk=run.pk).update(
            task_result=make_task_result(status="FAILED", exception_class_path="sessions.executor.run.RunStoppedError")
        )
        Run.objects.cancel(run)

        run = Run.objects.select_related("task_result").get(pk=run.pk)
        run.sync_and_save()

        run.refresh_from_db()
        assert run.is_cancelled
        assert run_finished_spy.call_args.kwargs["never_started"] is False

    def test_a_run_asked_once_is_not_asked_again(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.RUNNING)
        Run.objects.cancel(run)

        assert Run.objects.cancel(run) is False

    def test_a_finished_run_is_left_as_it_ended(self, run_finished_spy, badge):
        run = _create_run(status=RunStatus.SUCCESSFUL)

        assert Run.objects.cancel(run) is False

        run.refresh_from_db()
        assert (run.status, run.cancel_requested_at) == (RunStatus.SUCCESSFUL, None)
        assert not run_finished_spy.called


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("requested", [True, False])
async def test_acancel_requested_is_true_once_cancel_asked(requested):
    run = await sync_to_async(_create_run)(status=RunStatus.RUNNING)
    if requested:
        await sync_to_async(Run.objects.cancel)(run)

    assert await Run.objects.acancel_requested(str(run.pk)) is requested


@pytest.mark.django_db
class TestUpdateUnfinished:
    def test_a_finished_row_is_left_alone(self, badge):
        run = _create_run(status=RunStatus.SUCCESSFUL)

        assert Run.objects.update_unfinished(run, status=RunStatus.RUNNING) is False

        run.refresh_from_db()
        assert run.status == RunStatus.SUCCESSFUL
        badge.assert_not_called()

    def test_a_terminal_status_is_refused(self):
        run = _create_run(status=RunStatus.RUNNING)

        with pytest.raises(ValueError, match="is a terminal run status"):
            Run.objects.update_unfinished(run, status=RunStatus.FAILED)


@pytest.mark.django_db(transaction=True)
class TestAfinish:
    async def test_it_writes_refreshes_the_badge_and_emits(self, run_finished_spy):
        run = await sync_to_async(_create_run)()

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.WAITING_INPUT, question={"q": 1}) is True

        await run.arefresh_from_db()
        assert (run.status, run.question) == (RunStatus.WAITING_INPUT, {"q": 1})
        publish.assert_called_once()
        assert run_finished_spy.call_args.kwargs["run"] is run

    async def test_a_chat_run_refreshes_the_badge_without_emitting(self, run_finished_spy):
        run = await sync_to_async(_create_run)(trigger_type=SessionOrigin.CHAT)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.FAILED) is True

        publish.assert_called_once()
        assert not run_finished_spy.called

    async def test_the_worker_threads_connection_is_returned_even_when_a_receiver_raises(self):
        run = await sync_to_async(_create_run)(trigger_type=SessionOrigin.CHAT)

        with (
            patch("core.ui_events.publisher.runs_changed"),
            patch("sessions.managers.close_old_connections") as close,
            patch.object(type(Run.objects), "_emit_run_finished", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError),
        ):
            await Run.objects.afinish(run, RunStatus.SUCCESSFUL)

        close.assert_called_once_with()

    async def test_a_finished_row_is_never_overwritten(self, run_finished_spy):
        run = await sync_to_async(_create_run)()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.SUCCESSFUL) is False

        publish.assert_not_called()
        assert not run_finished_spy.called


@pytest.mark.django_db
class TestOwnedBy:
    def test_an_admin_gets_only_their_own_sessions(self, admin_user, other_user):
        mine = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, user=admin_user)
        Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, user=other_user)

        assert list(Session.objects.owned_by(admin_user)) == [mine]

    def test_a_run_actor_owns_the_shared_thread(self, other_user):
        session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.ISSUE_WEBHOOK)
        Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.ISSUE_WEBHOOK,
            repo_id="acme/api",
            external_username=other_user.username,
        )

        assert list(Session.objects.owned_by(other_user)) == [session]


@pytest.mark.django_db
class TestListedFor:
    def _session(self, user, **kwargs):
        return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, user=user, **kwargs)

    def test_archived_sessions_are_left_out_by_default(self, other_user):
        from django.utils import timezone

        shown = self._session(other_user)
        self._session(other_user, hidden_at=timezone.now())

        assert list(Session.objects.listed_for(other_user, mine=True)) == [shown]

    def test_archived_lists_only_the_archived_sessions(self, other_user):
        from django.utils import timezone

        self._session(other_user)
        hidden = self._session(other_user, hidden_at=timezone.now())

        assert list(Session.objects.listed_for(other_user, mine=True, archived=True)) == [hidden]
