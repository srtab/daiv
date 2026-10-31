from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import stale_cutoff
from sessions.inbox.complete import MAX_EVENT_ATTEMPTS, aon_run_ended, recover_stale_events
from sessions.models import EventFailure, EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def drain():
    with patch("sessions.inbox.complete.drain_logged") as drain:
        yield drain


async def _run_with_event(status: str, *, attempts: int = 0, **run_fields) -> tuple[Run, InboxEvent]:
    session = await amake_session()
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id, status=status,
        **run_fields,
    )  # fmt: skip
    event = await amake_event(session, EventKind.CI_FAILED, run=run, state=EventState.TAKEN, attempts=attempts)
    return run, event


async def _state(event: InboxEvent) -> InboxEvent:
    return await InboxEvent.objects.aget(pk=event.pk)


class TestOutcomes:
    @pytest.mark.parametrize("status", [RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT])
    async def test_a_run_that_did_its_job_handles_its_events(self, drain, status):
        run, event = await _run_with_event(status)

        await aon_run_ended(run)

        assert (await _state(event)).state == EventState.HANDLED
        drain.assert_called_once_with(run.session_id)

    async def test_a_run_its_owner_stopped_dismisses_its_events(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED, cancel_requested_at=timezone.now())

        await aon_run_ended(run)

        assert (await _state(event)).state == EventState.DISMISSED
        drain.assert_called_once_with(run.session_id)

    async def test_a_failed_run_puts_its_events_back_for_another_attempt(self, drain):
        run, event = await _run_with_event(RunStatus.FAILED)

        await aon_run_ended(run)

        stored = await _state(event)
        assert (stored.state, stored.attempts, stored.run_id) == (EventState.PENDING, 1, None)
        drain.assert_called_once_with(run.session_id)

    async def test_the_last_attempt_fails_the_events_and_logs(self, drain, caplog):
        run, event = await _run_with_event(RunStatus.FAILED, attempts=MAX_EVENT_ATTEMPTS - 1)

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await aon_run_ended(run)

        stored = await _state(event)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.RETRIES_EXHAUSTED)
        assert stored.attempts == MAX_EVENT_ATTEMPTS
        assert "failed after 3 attempts" in caplog.text
        drain.assert_called_once_with(run.session_id)

    async def test_events_the_run_did_not_take_are_left_alone(self, drain):
        run, _ = await _run_with_event(RunStatus.FAILED)
        pending = await amake_event(await Session.objects.aget(pk=run.session_id), EventKind.NOTICE, run=run)

        await aon_run_ended(run)

        assert (await _state(pending)).attempts == 0

    async def test_a_settling_failure_is_logged_and_still_drains(self, drain, caplog):
        run, _ = await _run_with_event(RunStatus.SUCCESSFUL)

        with (
            patch("sessions.inbox.complete.settle_events", side_effect=RuntimeError("db down")),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            await aon_run_ended(run)

        assert "settling the events" in caplog.text
        drain.assert_called_once_with(run.session_id)


class TestRecoverStaleEvents:
    async def test_events_of_a_run_that_died_with_its_slot_are_retried(self):
        run, event = await _run_with_event(RunStatus.RUNNING)
        await Session.objects.filter(pk=run.session_id).aupdate(
            active_run_id=str(run.pk), last_active_at=stale_cutoff() - timedelta(minutes=1)
        )

        assert await _recover() == 1

        stored = await _state(event)
        assert (stored.state, stored.attempts) == (EventState.PENDING, 1)

    async def test_events_of_a_run_that_ended_unsettled_follow_its_outcome(self):
        run, event = await _run_with_event(RunStatus.SUCCESSFUL)
        await Session.objects.filter(pk=run.session_id).aupdate(last_active_at=stale_cutoff() - timedelta(minutes=1))

        assert await _recover() == 1

        assert (await _state(event)).state == EventState.HANDLED

    async def test_events_on_a_live_slot_are_left_alone(self):
        run, event = await _run_with_event(RunStatus.RUNNING)

        assert await _recover() == 0

        assert (await _state(event)).state == EventState.TAKEN


async def _recover() -> int:
    return await sync_to_async(recover_stale_events)()
