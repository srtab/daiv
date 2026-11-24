import logging

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import DEAD_RUN_MESSAGE
from sessions.inbox.own_runs import OwnRunEvents
from sessions.models import EventFailure, EventKind, EventState, InboxEvent, MergeRequestState, Run, RunStatus

from core.constants import INTERRUPTED_MESSAGE
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session
from tests.unit_tests.webhooks.conftest import make_db_task_result

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def own_runs():
    calls: list[tuple] = []

    class Recorded(OwnRunEvents):
        kind = EventKind.LEG_FOLLOW_UP
        not_run = {EventFailure.MR_MERGED: "merged", EventFailure.SESSION_ARCHIVED: "archived"}
        state = MergeRequestState.OPEN

        @classmethod
        def merge_request_state(cls, session, event):
            return cls.state

        @classmethod
        def _stopped(cls, event, failure, run):
            calls.append(("stopped", event.pk, failure))

        @classmethod
        def _failed(cls, run, events):
            calls.append(("failed", run.pk, [event.pk for event in events]))

    Recorded.calls = calls
    return Recorded


async def _posted(**run_fields) -> tuple[InboxEvent, Run]:
    session = await amake_session()
    run = await Run.objects.acreate(
        session=session, trigger_type="api_job", repo_id=session.repo_id, status=RunStatus.QUEUED, **run_fields
    )
    event = await amake_event(
        session, EventKind.LEG_FOLLOW_UP, payload={"prompt": "Make the failing test pass", "batch_id": "b"}, run=run
    )
    return event, run


async def _failed(run: Run, **fields) -> Run:
    await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, **fields)
    return await Run.objects.aget(pk=run.pk)


class TestStopReason:
    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            (MergeRequestState.OPEN, None),
            (MergeRequestState.DRAFT, None),
            (MergeRequestState.MERGED, EventFailure.MR_MERGED),
            (MergeRequestState.CLOSED, EventFailure.MR_CLOSED),
        ],
    )
    async def test_its_merge_request_ending_stops_it(self, own_runs, state, reason):
        event, _ = await _posted()
        own_runs.state = state
        session = await sync_to_async(lambda: event.session)()

        assert own_runs.stop_reason(session, event) == reason

    async def test_an_archived_session_stops_it_whatever_its_merge_request(self, own_runs):
        event, _ = await _posted()
        session = await sync_to_async(lambda: event.session)()
        session.archived_at = timezone.now()

        assert own_runs.stop_reason(session, event) == EventFailure.SESSION_ARCHIVED


class TestStop:
    async def test_it_fails_the_event_and_finishes_its_run_never_started_with_why(self, own_runs, drain):
        event, run = await _posted()

        await sync_to_async(own_runs.stop)(event, EventFailure.MR_MERGED, run)

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure, stored.run_id) == (EventState.FAILED, EventFailure.MR_MERGED, run.pk)
        stopped = await Run.objects.aget(pk=run.pk)
        assert (stopped.status, stopped.error_message) == (RunStatus.FAILED, "merged")
        assert own_runs.calls == [("stopped", event.pk, EventFailure.MR_MERGED)]

    async def test_a_failure_it_does_not_name_ends_the_run_with_the_failures_label(self, own_runs, drain):
        event, run = await _posted()

        await sync_to_async(own_runs.stop)(event, EventFailure.ACCESS_DENIED, run)

        stopped = await Run.objects.aget(pk=run.pk)
        assert stopped.error_message == EventFailure.ACCESS_DENIED.label


class TestSettleUnstartable:
    async def test_a_run_that_is_gone_or_no_longer_queued_fails_it(self, own_runs):
        event, run = await _posted()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.READY)

        await sync_to_async(own_runs.settle_unstartable)(event, await Run.objects.aget(pk=run.pk))

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RUN_FAILED)
        assert own_runs.calls == []

    async def test_a_run_that_failed_settles_it_as_its_end_would(self, own_runs):
        event, run = await _posted()

        await sync_to_async(own_runs.settle_unstartable)(event, await _failed(run))

        assert own_runs.calls == [("failed", run.pk, [event.pk])]


class TestEndWithRun:
    async def test_a_failed_run_fails_its_event_for_good_and_reports_it_once(self, own_runs, caplog):
        event, run = await _posted()
        failed = await _failed(run)

        await sync_to_async(own_runs.end_with_run)(failed)
        await sync_to_async(own_runs.end_with_run)(failed)

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RUN_FAILED)
        assert own_runs.calls == [("failed", run.pk, [event.pk])]
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    async def test_a_run_reaped_after_its_worker_died_is_logged_as_an_error_once(self, own_runs, caplog):
        event, run = await _posted()
        failed = await _failed(run, error_message=DEAD_RUN_MESSAGE, task_result_id=await make_db_task_result())

        await sync_to_async(own_runs.end_with_run)(failed)
        await sync_to_async(own_runs.end_with_run)(failed)

        assert own_runs.calls == [("failed", run.pk, [event.pk])]
        assert [record.levelno for record in caplog.records if str(run.pk) in record.getMessage()] == [logging.ERROR]

    async def test_a_run_a_restart_interrupted_is_not_logged_as_an_error(self, own_runs, caplog):
        event, run = await _posted()
        failed = await _failed(run, error_message=INTERRUPTED_MESSAGE, task_result_id=await make_db_task_result())

        await sync_to_async(own_runs.end_with_run)(failed)

        assert own_runs.calls == [("failed", run.pk, [event.pk])]
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    async def test_a_cancelled_run_dismisses_its_event_without_reporting_it(self, own_runs):
        event, run = await _posted()

        await sync_to_async(own_runs.end_with_run)(await _failed(run, cancel_requested_at=timezone.now()))

        assert (await InboxEvent.objects.aget(pk=event.pk)).state == EventState.DISMISSED
        assert own_runs.calls == []

    async def test_a_run_that_succeeded_leaves_its_event_to_the_shared_path(self, own_runs):
        event, run = await _posted()
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.SUCCESSFUL)

        await sync_to_async(own_runs.end_with_run)(await Run.objects.aget(pk=run.pk))

        assert (await InboxEvent.objects.aget(pk=event.pk)).state == EventState.PENDING
        assert own_runs.calls == []
