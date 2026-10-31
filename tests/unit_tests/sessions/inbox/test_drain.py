from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sessions.executor.lock import stale_cutoff
from sessions.inbox import kinds
from sessions.inbox.drain import adrain
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    QuietReason,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import EnqueueFailure

from codebase.authorization import RepositoryAccessDenied
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, ci_payload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def enqueue():
    with patch("sessions.inbox.drain.aenqueue_run", new=AsyncMock(return_value=None)) as enqueue:
        yield enqueue


async def _state(event: InboxEvent) -> InboxEvent:
    return await InboxEvent.objects.aget(pk=event.pk)


async def _queued_next_run(session: Session, *, ref: str | None = None, trigger=SessionOrigin.PIPELINE_WEBHOOK) -> Run:
    return await Run.objects.acreate(
        session=session, trigger_type=trigger, repo_id=session.repo_id, ref=session.ref if ref is None else ref,
        status=RunStatus.QUEUED,
    )  # fmt: skip


class TestSlot:
    async def test_a_live_holder_keeps_the_events_pending(self, enqueue):
        session = await amake_session(active_run_id="chat-run")
        event = await amake_event(session)

        assert await adrain(session.thread_id) is None
        assert (await _state(event)).state == EventState.PENDING
        enqueue.assert_not_awaited()

    async def test_a_stale_holder_counts_as_free(self, enqueue):
        session = await amake_session(active_run_id="gone", last_active_at=stale_cutoff() - timedelta(minutes=1))
        await amake_event(session)

        run = await adrain(session.thread_id)

        assert run is not None
        assert (await Session.objects.aget(pk=session.pk)).active_run_id == str(run.pk)

    async def test_a_missing_session_starts_nothing(self, enqueue):
        assert await adrain("no-such-thread") is None


class TestTaking:
    async def test_the_run_claims_the_slot_and_takes_the_events(self, enqueue):
        session = await amake_session()
        failure = await amake_event(session, merge_request_iid=9)
        note = await amake_event(session, EventKind.NOTICE)

        run = await adrain(session.thread_id)

        assert run.status == RunStatus.READY
        assert run.trigger_type == SessionOrigin.PIPELINE_WEBHOOK
        assert run.ref == session.ref
        assert run.merge_request_iid == 9
        assert "CI failed on this merge request" in run.prompt
        assert (await Session.objects.aget(pk=session.pk)).active_run_id == str(run.pk)
        for event in (failure, note):
            stored = await _state(event)
            assert (stored.state, stored.run_id) == (EventState.TAKEN, run.pk)
        enqueue.assert_awaited_once()
        assert enqueue.await_args.args[0].pk == run.pk

    async def test_the_queued_next_run_is_promoted_with_the_highest_precedence_trigger(self, enqueue):
        session = await amake_session()
        next_run = await _queued_next_run(session)
        await amake_event(session, EventKind.CI_FAILED, run=next_run)
        await amake_event(session, EventKind.MENTION, run=next_run, merge_request_iid=9)

        run = await adrain(session.thread_id)

        assert run.pk == next_run.pk
        stored = await Run.objects.aget(pk=next_run.pk)
        assert stored.status == RunStatus.READY
        assert stored.trigger_type == SessionOrigin.MR_WEBHOOK
        assert await Run.objects.filter(session=session).acount() == 1

    async def test_a_chat_message_gives_the_run_its_message_id_and_override(self, enqueue):
        session = await amake_session()
        payload = {"text": "hi", "agui_message_id": "msg-1", "agent_model": "openrouter:x", "agent_thinking_level": ""}
        await amake_event(session, EventKind.CHAT_MESSAGE, payload=payload)

        run = await adrain(session.thread_id)

        assert (run.trigger_type, run.message_id, run.agent_model) == (SessionOrigin.CHAT, "msg-1", "openrouter:x")

    async def test_older_events_of_a_subject_are_superseded_by_the_newest(self, enqueue):
        session = await amake_session()
        older = await amake_event(session, subject=kinds.ci_subject(9), payload=ci_payload(1))
        newer = await amake_event(session, subject=kinds.ci_subject(9), payload=ci_payload(2))

        run = await adrain(session.thread_id)

        assert (await _state(older)).state == EventState.SUPERSEDED
        assert (await _state(older)).run_id == run.pk
        assert (await _state(newer)).state == EventState.TAKEN

    async def test_only_the_first_waking_events_branch_is_taken(self, enqueue):
        session = await amake_session()
        first = await amake_event(session, ref="chore/x")
        other = await amake_event(session, EventKind.CHAT_MESSAGE, ref="feat/y")
        anywhere = await amake_event(session, EventKind.NOTICE, ref="")

        run = await adrain(session.thread_id)

        assert run.ref == "chore/x"
        assert (await _state(first)).state == EventState.TAKEN
        assert (await _state(anywhere)).state == EventState.TAKEN
        assert (await _state(other)).state == EventState.PENDING

    async def test_a_legacy_queued_job_run_is_never_promoted(self, enqueue):
        session = await amake_session()
        legacy = await _queued_next_run(session, trigger=SessionOrigin.API_JOB)
        await amake_event(session)

        run = await adrain(session.thread_id)

        assert run.pk != legacy.pk
        assert (await Run.objects.aget(pk=legacy.pk)).status == RunStatus.QUEUED


class TestNothingToWake:
    async def test_no_waking_event_starts_nothing(self, enqueue):
        session = await amake_session()
        note = await amake_event(session, EventKind.NOTICE)

        assert await adrain(session.thread_id) is None
        assert (await _state(note)).state == EventState.PENDING
        assert (await Session.objects.aget(pk=session.pk)).active_run_id is None

    async def test_a_next_run_left_without_a_waking_event_is_deleted(self, enqueue):
        session = await amake_session()
        next_run = await _queued_next_run(session)
        failure = await amake_event(session, run=next_run, subject=kinds.ci_subject(9), payload=ci_payload(1))
        passed = await amake_event(session, EventKind.CI_PASSED, run=None, subject=kinds.ci_subject(9))

        assert await adrain(session.thread_id) is None

        assert not await Run.objects.filter(pk=next_run.pk).aexists()
        assert (await _state(failure)).state == EventState.SUPERSEDED
        assert (await _state(failure)).run_id is None
        assert (await _state(passed)).state == EventState.PENDING

    @pytest.mark.parametrize(
        ("session_fields", "reason"), [({"inbox_muted": True}, QuietReason.MUTED), ({}, QuietReason.RUN_LIMIT)]
    )
    async def test_a_blocked_delegation_wake_is_only_remembered(self, enqueue, session_fields, reason):
        session = await amake_session(**session_fields)
        if reason == QuietReason.RUN_LIMIT:
            for _ in range(3):
                await Run.objects.acreate(
                    session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id,
                    status=RunStatus.SUCCESSFUL,
                )  # fmt: skip
        finished = await amake_event(session, EventKind.DELEGATION_FINISHED)

        assert await adrain(session.thread_id) is None

        stored = await _state(finished)
        assert (stored.wake, stored.reason, stored.state) == (False, reason, EventState.PENDING)


class TestAccess:
    async def test_an_owner_without_access_fails_the_events_and_starts_nothing(self, enqueue, member_user):
        session = await amake_session(user=member_user)
        next_run = await _queued_next_run(session)
        event = await amake_event(session, run=next_run)
        denied = Mock(side_effect=RepositoryAccessDenied([session.repo_id]))

        with patch("sessions.admission.assert_can_run", denied):
            assert await adrain(session.thread_id) is None

        denied.assert_called_once_with(member_user, [session.repo_id])
        stored = await _state(event)
        assert (stored.state, stored.failure) == (EventState.FAILED, EventFailure.ACCESS_DENIED)
        assert not await Run.objects.filter(pk=next_run.pk).aexists()
        assert (await Session.objects.aget(pk=session.pk)).active_run_id is None
        enqueue.assert_not_awaited()

    async def test_an_owner_with_access_starts_the_run(self, enqueue, admin_user):
        session = await amake_session(user=admin_user)
        await amake_event(session)

        run = await adrain(session.thread_id)

        assert run.user_id == admin_user.pk

    async def test_an_ownerless_session_skips_the_check(self, enqueue):
        session = await amake_session(user=None)
        await amake_event(session)

        assert await adrain(session.thread_id) is not None


class TestEnqueueFailure:
    async def test_it_frees_the_slot_and_puts_the_events_back(self, caplog):
        session = await amake_session()
        event = await amake_event(session)

        with (
            patch("sessions.inbox.drain.aenqueue_run", new=AsyncMock(return_value=EnqueueFailure(task_enqueued=False))),
            caplog.at_level("ERROR", logger="daiv.sessions"),
        ):
            assert await adrain(session.thread_id) is None

        assert (await Session.objects.aget(pk=session.pk)).active_run_id is None
        stored = await _state(event)
        assert (stored.state, stored.run_id) == (EventState.PENDING, None)
        assert "could not enqueue" in caplog.text


class TestCiAttempts:
    async def test_taking_a_waking_ci_failure_spends_an_attempt_on_its_commit(self, enqueue):
        session = await amake_session()
        ownership = await MergeRequestOwnership.objects.acreate(
            repo_id=session.repo_id, merge_request_iid=9, session=session, source_branch=session.ref, attempts=1
        )
        await amake_event(session, merge_request_iid=9, payload=ci_payload(sha="e96f1f77"))

        await adrain(session.thread_id)

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_examined_sha) == (2, "e96f1f77")

    async def test_a_quiet_ci_failure_spends_nothing(self, enqueue):
        session = await amake_session()
        ownership = await MergeRequestOwnership.objects.acreate(
            repo_id=session.repo_id, merge_request_iid=9, session=session, source_branch=session.ref
        )
        await amake_event(session, EventKind.CHAT_MESSAGE)
        await amake_event(session, merge_request_iid=9, wake=False, reason=QuietReason.ALREADY_EXAMINED)

        await adrain(session.thread_id)

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_examined_sha) == (0, "")
