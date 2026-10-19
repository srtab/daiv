import uuid
from unittest.mock import MagicMock, patch

from django.db.models import Q
from django.test import TestCase

import pytest
from asgiref.sync import sync_to_async
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signal_defs import run_finished


def _create_run(*, status: str = RunStatus.RUNNING, trigger_type: str = SessionOrigin.PIPELINE_WEBHOOK) -> Run:
    session = Session.objects.create(thread_id=str(uuid.uuid4()), origin=trigger_type, repo_id="acme/api")
    return Run.objects.create(session=session, trigger_type=trigger_type, repo_id="acme/api", status=status)


@pytest.fixture
def received():
    receiver = MagicMock()
    run_finished.connect(receiver, dispatch_uid="test-managers-received")
    yield receiver
    run_finished.disconnect(dispatch_uid="test-managers-received")


@pytest.fixture
def badge():
    with patch("core.ui_events.publisher.runs_changed") as publish:
        yield publish


@pytest.mark.django_db
class TestFinish:
    def test_it_writes_the_status_and_fields_and_updates_the_instance(self, received, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="boom") is True

        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.FAILED, "boom")
        badge.assert_called_once()
        assert received.call_args.kwargs["run"] is run
        assert received.call_args.kwargs["skip_dispatch"] is False

    def test_the_badge_refresh_waits_for_the_commit(self, badge):
        run = _create_run()

        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            Run.objects.finish(run, RunStatus.SUCCESSFUL)
        badge.assert_not_called()
        for callback in callbacks:
            callback()
        badge.assert_called_once()

    def test_it_forwards_skip_dispatch(self, received):
        run = _create_run()

        Run.objects.finish(run, RunStatus.FAILED, skip_dispatch=True)

        assert received.call_args.kwargs["skip_dispatch"] is True

    def test_a_chat_run_refreshes_the_badge_without_emitting(self, received, badge):
        run = _create_run(trigger_type=SessionOrigin.CHAT)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True

        badge.assert_called_once()
        assert not received.called

    def test_a_finished_row_is_never_overwritten(self, received, badge):
        run = _create_run()
        Run.objects.filter(pk=run.pk).update(status=RunStatus.SUCCESSFUL)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.FAILED, error_message="late") is False

        run.refresh_from_db()
        assert (run.status, run.error_message) == (RunStatus.SUCCESSFUL, "")
        badge.assert_not_called()
        assert not received.called

    def test_a_row_failing_the_guard_is_left_alone(self, received):
        run = _create_run(status=RunStatus.RUNNING)

        assert Run.objects.finish(run, RunStatus.FAILED, where=Q(status=RunStatus.READY)) is False

        assert run.status == RunStatus.RUNNING
        run.refresh_from_db()
        assert run.status == RunStatus.RUNNING
        assert not received.called

    def test_a_non_terminal_status_is_refused(self):
        run = _create_run(status=RunStatus.READY)

        with pytest.raises(ValueError, match="not a terminal run status"):
            Run.objects.finish(run, RunStatus.RUNNING)

    def test_a_failing_receiver_neither_stops_the_others_nor_the_finish(self, caplog):
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
        assert "failed for run_finished" in caplog.text


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
    async def test_it_writes_refreshes_the_badge_and_emits(self, received):
        run = await sync_to_async(_create_run)()

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.WAITING_INPUT, question={"q": 1}) is True

        await run.arefresh_from_db()
        assert (run.status, run.question) == (RunStatus.WAITING_INPUT, {"q": 1})
        publish.assert_called_once()
        assert received.call_args.kwargs["run"] is run

    async def test_a_chat_run_refreshes_the_badge_without_emitting(self, received):
        run = await sync_to_async(_create_run)(trigger_type=SessionOrigin.CHAT)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.FAILED) is True

        publish.assert_called_once()
        assert not received.called

    async def test_a_finished_row_is_never_overwritten(self, received):
        run = await sync_to_async(_create_run)()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)

        with patch("core.ui_events.publisher.runs_changed") as publish:
            assert await Run.objects.afinish(run, RunStatus.SUCCESSFUL) is False

        publish.assert_not_called()
        assert not received.called
