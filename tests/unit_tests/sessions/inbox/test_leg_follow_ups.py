import uuid
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.inbox.drain import Drain
from sessions.inbox.leg_follow_ups import BUSY, LegFollowUpRefused, LegFollowUps
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.ownership import Ownerships
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import dispatch_next_queued

from tests.unit_tests.sessions.inbox.conftest import amake_event, make_leg_session, post_leg_follow_up

pytestmark = pytest.mark.django_db(transaction=True)

REFERENCE = {"kind": "issue", "repo_id": "acme/api", "iid": 12}


@pytest.fixture
def drain():
    with patch.object(Drain, "start_logged", autospec=True) as drain:
        yield drain


async def _leg_session(user, **fields):
    return await sync_to_async(make_leg_session)(user, **fields)


async def _post(session, user, **fields) -> Run:
    return await sync_to_async(post_leg_follow_up)(session, user, **fields)


class TestPost:
    async def test_it_creates_the_event_and_its_queued_delegated_run_linked(self, admin_user, drain):
        session = await _leg_session(admin_user)
        batch = uuid.uuid4()

        run = await _post(session, admin_user, batch_id=batch, muted=True, references=[REFERENCE])

        stored = await Run.objects.aget(pk=run.pk)
        assert (stored.trigger_type, stored.status, stored.batch_id) == (
            SessionOrigin.DELEGATED_JOB, RunStatus.QUEUED, batch
        )  # fmt: skip
        assert (stored.user_id, stored.muted, stored.prompt) == (admin_user.pk, True, "Make the failing test pass")
        assert (stored.session_id, stored.repo_id, stored.ref) == (session.pk, "acme/api", "daiv/leg-branch")
        event = await InboxEvent.objects.aget(run=run)
        assert (event.kind, event.wake, event.state, event.author_id, event.ref) == (
            EventKind.LEG_FOLLOW_UP, True, EventState.PENDING, admin_user.pk, "daiv/leg-branch"
        )  # fmt: skip
        assert event.source_key == f"leg-follow-up:{batch}"
        assert event.payload == {
            "prompt": "Make the failing test pass",
            "batch_id": str(batch),
            "references": [REFERENCE],
            "muted": True,
        }
        drain.assert_not_called()

    async def test_a_payload_without_a_mute_or_references_leaves_them_out(self, admin_user, drain):
        session = await _leg_session(admin_user)

        run = await _post(session, admin_user)

        assert set((await InboxEvent.objects.aget(run=run)).payload) == {"prompt", "batch_id"}

    @pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.READY, RunStatus.RUNNING])
    async def test_a_leg_still_working_is_refused_and_nothing_is_created(self, admin_user, drain, status):
        session = await _leg_session(admin_user, status=status)

        with pytest.raises(LegFollowUpRefused) as refused:
            await _post(session, admin_user)

        assert refused.value.reason == BUSY
        assert not await InboxEvent.objects.filter(session=session).aexists()
        assert await Run.objects.filter(session=session).acount() == 1

    async def test_a_leg_with_a_follow_up_waiting_is_refused(self, admin_user, drain):
        session = await _leg_session(admin_user)
        await _post(session, admin_user)

        with pytest.raises(LegFollowUpRefused):
            await _post(session, admin_user)

        assert await InboxEvent.objects.filter(session=session).acount() == 1

    @pytest.mark.parametrize("merged", [True, False], ids=["merged", "closed"])
    async def test_a_merged_or_closed_merge_request_is_refused(self, admin_user, drain, merged):
        session = await _leg_session(admin_user, merge_request_iid=4)
        await MergeRequestOwnership.objects.acreate(
            repo_id="acme/api", merge_request_iid=4, session=session, source_branch="daiv/leg-branch"
        )
        await Ownerships("acme/api").aclose(4, merged=merged, by_username="sfr", target_branch="main")

        with pytest.raises(LegFollowUpRefused) as refused:
            await _post(session, admin_user)

        assert refused.value.reason == (MergeRequestState.MERGED if merged else MergeRequestState.CLOSED)
        assert not await InboxEvent.objects.filter(session=session, kind=EventKind.LEG_FOLLOW_UP).aexists()

    async def test_its_run_is_no_next_run_and_the_queue_leaves_it_queued(self, admin_user, drain):
        session = await _leg_session(admin_user)
        run = await _post(session, admin_user)

        assert not await sync_to_async(lambda: list(NextRuns(session).queued()))()
        with patch("sessions.queue.run_job_task") as task:
            task.aenqueue = AsyncMock()
            await sync_to_async(dispatch_next_queued)(session.pk)

        task.aenqueue.assert_not_awaited()
        assert (await Run.objects.aget(pk=run.pk)).status == RunStatus.QUEUED


class TestStopReason:
    async def test_none_while_the_merge_request_is_open_or_unknown(self, admin_user):
        session = await _leg_session(admin_user, merge_request_iid=4)

        assert await sync_to_async(LegFollowUps.stop_reason)(session) is None

    @pytest.mark.parametrize(
        ("merged", "failure"),
        [(True, EventFailure.MR_MERGED), (False, EventFailure.MR_CLOSED)],
        ids=["merged", "closed"],
    )
    async def test_the_merge_requests_end_once_ownership_records_it(self, admin_user, merged, failure):
        session = await _leg_session(admin_user, merge_request_iid=4)
        await MergeRequestOwnership.objects.acreate(
            repo_id="acme/api", merge_request_iid=4, session=session, source_branch="daiv/leg-branch"
        )
        await Ownerships("acme/api").aclose(4, merged=merged, by_username="sfr", target_branch="main")

        assert await sync_to_async(LegFollowUps.stop_reason)(session) == failure


class TestEndWithRun:
    async def _follow_up(self, admin_user) -> tuple[Run, InboxEvent]:
        session = await _leg_session(admin_user)
        run = await _post(session, admin_user)
        return run, await InboxEvent.objects.aget(run=run)

    async def test_a_waiting_follow_up_whose_run_was_cancelled_is_dismissed(self, admin_user, drain):
        run, event = await self._follow_up(admin_user)

        assert await sync_to_async(Run.objects.cancel)(run)

        assert (await InboxEvent.objects.aget(pk=event.pk)).state == EventState.DISMISSED

    async def test_a_waiting_follow_up_whose_run_failed_fails_with_it(self, admin_user, drain):
        run, event = await self._follow_up(admin_user)

        await Run.objects.afinish(
            run, RunStatus.FAILED, error_message="enqueue_failed: boom", finished_at=timezone.now()
        )

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure, stored.run_id) == (EventState.FAILED, EventFailure.RUN_FAILED, run.pk)

    async def test_a_taken_follow_up_whose_run_failed_fails_with_it(self, admin_user, drain):
        run, event = await self._follow_up(admin_user)
        await InboxEvent.objects.filter(pk=event.pk).aupdate(state=EventState.TAKEN)
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED)
        run = await Run.objects.aget(pk=run.pk)

        await sync_to_async(LegFollowUps.end_with_run)(run)

        stored = await InboxEvent.objects.aget(pk=event.pk)
        assert (stored.state, stored.failure, stored.attempts) == (EventState.FAILED, EventFailure.RUN_FAILED, 0)

    async def test_every_other_event_on_the_session_is_left_alone(self, admin_user, drain):
        run, _ = await self._follow_up(admin_user)
        session = await Session.objects.aget(pk=run.session_id)
        others = [
            await amake_event(session, EventKind.CI_FAILED),
            await amake_event(session, EventKind.BRANCH_PUSHED, payload={
                "before": "a" * 8, "after": "b" * 8, "commit_count": 1, "pusher_username": "bob"
            }, run=run, state=EventState.TAKEN),
        ]  # fmt: skip
        await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.FAILED, cancel_requested_at=timezone.now())

        await sync_to_async(LegFollowUps.end_with_run)(await Run.objects.aget(pk=run.pk))

        assert [(await InboxEvent.objects.aget(pk=event.pk)).state for event in others] == [
            EventState.PENDING, EventState.TAKEN
        ]  # fmt: skip
