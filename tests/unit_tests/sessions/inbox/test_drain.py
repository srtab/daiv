from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import stale_cutoff
from sessions.inbox import kinds
from sessions.inbox.drain import adrain, atake_for_chat, keep_queued_claims_alive
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
from tests.unit_tests.sessions.conftest import make_task_result
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, ci_payload

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def enqueue():
    with patch("sessions.inbox.drain.aenqueue_run", new=AsyncMock(return_value=None)) as enqueue:
        yield enqueue


async def _state(event: InboxEvent) -> InboxEvent:
    return await InboxEvent.objects.aget(pk=event.pk)


async def _queued_next_run(
    session: Session, *, ref: str | None = None, trigger=SessionOrigin.PIPELINE_WEBHOOK, **fields
) -> Run:
    return await Run.objects.acreate(
        session=session, trigger_type=trigger, repo_id=session.repo_id, ref=session.ref if ref is None else ref,
        status=RunStatus.QUEUED, **fields,
    )  # fmt: skip


async def _end(run: Run) -> None:
    await Run.objects.filter(pk=run.pk).aupdate(status=RunStatus.SUCCESSFUL)
    await Session.objects.filter(pk=run.session_id).aupdate(active_run_id=None)


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
        other = await amake_event(
            session, EventKind.FOLLOW_UP_JOB, ref="feat/y",
            payload={"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB, "ref": "feat/y"},
        )  # fmt: skip
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

    async def test_the_run_limit_is_reported_once_the_block_commits(self, enqueue):
        session = await amake_session()
        for _ in range(3):
            await Run.objects.acreate(
                session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=session.repo_id,
                status=RunStatus.SUCCESSFUL,
            )  # fmt: skip
        await amake_event(session, EventKind.DELEGATION_FINISHED)

        with patch("sessions.inbox.drain.report_run_limit") as report:
            await adrain(session.thread_id)

        assert report.call_args.args[0].pk == session.pk


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

    async def test_a_mention_is_held_to_no_repository_check(self, enqueue, member_user):
        session = await amake_session(user=None)
        await amake_event(session, EventKind.MENTION, author=member_user)
        denied = Mock(side_effect=RepositoryAccessDenied([session.repo_id]))

        with patch("sessions.admission.assert_can_run", denied):
            run = await adrain(session.thread_id)

        assert run.user_id == member_user.pk
        denied.assert_not_called()

    async def test_a_denied_requester_fails_only_their_own_requests(self, enqueue, member_user, admin_user):
        session = await amake_session(user=admin_user)
        follow_up = await amake_event(session, EventKind.FOLLOW_UP_JOB, author=member_user)
        failure = await amake_event(session)
        comment = await amake_event(session, EventKind.COMMENT)

        def deny_member(user, repo_ids):
            if user == member_user:
                raise RepositoryAccessDenied(repo_ids)

        with patch("sessions.admission.assert_can_run", Mock(side_effect=deny_member)):
            await adrain(session.thread_id)

        assert (await _state(follow_up)).failure == EventFailure.ACCESS_DENIED
        run = await Run.objects.aget(session=session, status=RunStatus.READY)
        assert run.user_id == admin_user.pk
        assert {(await _state(failure)).run_id, (await _state(comment)).run_id} == {run.pk}


class TestActingUser:
    async def test_the_person_who_woke_the_run_is_whom_it_acts_as(self, enqueue, member_user, admin_user):
        session = await amake_session(user=admin_user)
        await amake_event(session, EventKind.MENTION, author=member_user)

        run = await adrain(session.thread_id)

        assert run.user_id == member_user.pk

    async def test_a_person_without_an_account_acts_as_no_one(self, enqueue, admin_user):
        session = await amake_session(user=admin_user)
        await amake_event(
            session, EventKind.MENTION, author=None, author_username="stranger", author_provider="gitlab",
            author_uid="77",
        )  # fmt: skip

        run = await adrain(session.thread_id)

        assert (run.user_id, run.external_username, run.external_provider, run.external_uid) == (
            None,
            "stranger",
            "gitlab",
            "77",
        )

    async def test_requests_from_two_people_run_separately_each_as_its_requester(
        self, enqueue, member_user, admin_user
    ):
        session = await amake_session(user=admin_user)
        chat = await amake_event(session, EventKind.CHAT_MESSAGE, author=admin_user)
        mention = await amake_event(session, EventKind.MENTION, author=member_user)

        first = await adrain(session.thread_id)
        await _end(first)
        second = await adrain(session.thread_id)

        assert (first.user_id, (await _state(chat)).run_id) == (admin_user.pk, first.pk)
        assert (second.user_id, (await _state(mention)).run_id) == (member_user.pk, second.pk)

    async def test_a_queued_next_run_keeps_the_requester_it_was_queued_for(self, enqueue, admin_user):
        session = await amake_session(user=admin_user)
        await amake_event(session, EventKind.MENTION, author=None, author_username="stranger")
        job = await _queued_next_run(session, trigger=SessionOrigin.API_JOB, user=admin_user)
        follow_up = await amake_event(session, EventKind.FOLLOW_UP_JOB, author=admin_user, run=job)

        run = await adrain(session.thread_id)

        assert (run.pk != job.pk, run.user_id) == (True, None)
        job = await Run.objects.aget(pk=job.pk)
        assert (job.status, job.user_id) == (RunStatus.QUEUED, admin_user.pk)
        assert ((await _state(follow_up)).state, (await _state(follow_up)).run_id) == (EventState.PENDING, job.pk)

    async def test_a_ci_failure_acts_as_the_owner(self, enqueue, admin_user):
        session = await amake_session(user=admin_user)
        await amake_event(session)

        assert (await adrain(session.thread_id)).user_id == admin_user.pk

    async def test_a_finished_batch_acts_as_its_delegator_with_its_mute(self, enqueue, admin_user):
        session = await amake_session(user=None)
        await amake_event(
            session,
            EventKind.DELEGATION_FINISHED,
            author=admin_user,
            payload={"batch_id": "b", "summary": "s", "muted": True},
        )

        run = await adrain(session.thread_id)

        assert (run.user_id, run.muted) == (admin_user.pk, True)

    async def test_an_ownerless_session_checks_the_user_the_run_acts_as(self, enqueue, member_user):
        session = await amake_session(user=None)
        event = await amake_event(session, EventKind.DELEGATION_FINISHED, author=member_user)
        denied = Mock(side_effect=RepositoryAccessDenied([session.repo_id]))

        with patch("sessions.admission.assert_can_run", denied):
            assert await adrain(session.thread_id) is None

        denied.assert_called_once_with(member_user, [session.repo_id])
        assert (await _state(event)).failure == EventFailure.ACCESS_DENIED


class TestSessionBranch:
    async def test_a_chat_message_follows_the_session_to_the_branch_its_last_run_moved_to(self, enqueue):
        session = await amake_session(ref="chore/x")
        next_run = await _queued_next_run(session, trigger=SessionOrigin.CHAT)
        chat = await amake_event(session, EventKind.CHAT_MESSAGE, run=next_run)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")

        run = await adrain(session.thread_id)

        assert (run.pk, run.ref) == (next_run.pk, "daiv/new-branch")
        assert (await _state(chat)).run_id == run.pk

    async def test_a_follow_up_keeps_its_job_id_when_it_follows_the_session_to_a_new_branch(self, enqueue):
        session = await amake_session(ref="chore/x")
        job = await _queued_next_run(session, trigger=SessionOrigin.API_JOB)
        payload = {"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB, "ref": ""}
        await amake_event(session, EventKind.FOLLOW_UP_JOB, payload=payload, run=job)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")

        run = await adrain(session.thread_id)

        assert (run.pk, run.ref) == (job.pk, "daiv/new-branch")

    async def test_a_next_run_another_request_holds_on_its_branch_stays_there(self, enqueue):
        session = await amake_session(ref="chore/x")
        next_run = await _queued_next_run(session, trigger=SessionOrigin.CHAT)
        chat = await amake_event(session, EventKind.CHAT_MESSAGE, run=next_run)
        payload = {"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB, "ref": "chore/x"}
        pinned = await amake_event(session, EventKind.FOLLOW_UP_JOB, payload=payload, run=next_run)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")

        run = await adrain(session.thread_id)

        assert (run.ref, (await _state(chat)).run_id) == ("daiv/new-branch", run.pk)
        assert run.pk != next_run.pk
        assert ((await _state(pinned)).run_id, (await Run.objects.aget(pk=next_run.pk)).ref) == (next_run.pk, "chore/x")

    async def test_a_next_run_whose_place_on_the_new_branch_is_taken_lets_its_requests_go(self, enqueue):
        session = await amake_session(ref="chore/x")
        stale = await _queued_next_run(session, trigger=SessionOrigin.CHAT)
        chat = await amake_event(session, EventKind.CHAT_MESSAGE, run=stale)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")
        current = await _queued_next_run(session, ref="daiv/new-branch", trigger=SessionOrigin.CHAT)
        later = await amake_event(session, EventKind.CHAT_MESSAGE, run=current)

        run = await adrain(session.thread_id)

        assert run.pk == current.pk
        assert ((await _state(chat)).run_id, (await _state(later)).run_id) == (current.pk, current.pk)
        assert not await Run.objects.filter(pk=stale.pk).aexists()

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            (EventKind.FOLLOW_UP_JOB, {"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB, "ref": ""}),
            (EventKind.DELEGATION_FINISHED, {"batch_id": "b", "summary": "s"}),
        ],
        ids=["follow-up-without-a-branch", "finished-batch"],
    )
    async def test_a_request_without_a_branch_of_its_own_follows_the_session_too(self, enqueue, kind, payload):
        session = await amake_session(ref="chore/x")
        event = await amake_event(session, kind, payload=payload)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")

        run = await adrain(session.thread_id)

        assert (run.ref, (await _state(event)).run_id) == ("daiv/new-branch", run.pk)

    async def test_a_follow_up_sent_for_a_branch_stays_on_it(self, enqueue):
        session = await amake_session(ref="chore/x")
        payload = {"prompt": "Bump it", "trigger_type": SessionOrigin.API_JOB, "ref": "chore/x"}
        await amake_event(session, EventKind.FOLLOW_UP_JOB, payload=payload)
        await Session.objects.filter(pk=session.pk).aupdate(ref="daiv/new-branch")

        assert (await adrain(session.thread_id)).ref == "chore/x"

    async def test_a_chat_turn_supersedes_an_older_failure_with_the_pass_it_takes(self):
        session = await amake_session()
        failure = await amake_event(session, merge_request_iid=9, subject=kinds.ci_subject(9))
        passed = await amake_event(session, EventKind.CI_PASSED, merge_request_iid=9, subject=kinds.ci_subject(9))

        taken = await atake_for_chat(session.thread_id, holder_id="chat-run")

        assert [event.pk for event in taken] == [passed.pk]
        assert (await _state(failure)).state == EventState.SUPERSEDED


class TestChatTurnTake:
    async def test_it_drops_a_next_run_its_supersede_leaves_with_nothing_to_wake_it(self):
        session = await amake_session()
        emptied = await _queued_next_run(session)
        await amake_event(session, merge_request_iid=9, subject=kinds.ci_subject(9), run=emptied)
        kept = await _queued_next_run(session, ref="feat/y", trigger=SessionOrigin.API_JOB)
        await amake_event(session, EventKind.FOLLOW_UP_JOB, ref="feat/y", run=kept)
        await amake_event(session, EventKind.CI_PASSED, merge_request_iid=9, subject=kinds.ci_subject(9))

        await atake_for_chat(session.thread_id, holder_id="chat-run")

        assert not await Run.objects.filter(pk=emptied.pk).aexists()
        assert await Run.objects.filter(pk=kept.pk).aexists()


class TestQueuedClaims:
    async def test_a_claim_whose_task_waits_for_a_worker_is_kept_alive(self):
        stale = stale_cutoff() - timedelta(minutes=5)
        task_result = await sync_to_async(make_task_result)(status="READY")
        session = await amake_session(last_active_at=stale)
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id=session.repo_id, status=RunStatus.READY,
            task_result=task_result,
        )  # fmt: skip
        await Session.objects.filter(pk=session.pk).aupdate(active_run_id=str(run.pk))
        other = await amake_session(active_run_id="chat-run", last_active_at=stale)

        assert await sync_to_async(keep_queued_claims_alive)() == 1

        assert (await Session.objects.aget(pk=session.pk)).last_active_at > stale_cutoff()
        assert (await Session.objects.aget(pk=other.pk)).last_active_at == stale


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

    async def test_it_spends_no_ci_attempt(self):
        session = await amake_session()
        ownership = await MergeRequestOwnership.objects.acreate(
            repo_id=session.repo_id, merge_request_iid=9, session=session, source_branch=session.ref
        )
        await amake_event(session, merge_request_iid=9)

        with patch(
            "sessions.inbox.drain.aenqueue_run", new=AsyncMock(return_value=EnqueueFailure(task_enqueued=False))
        ):
            await adrain(session.thread_id)

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_examined_sha) == (0, "")


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
