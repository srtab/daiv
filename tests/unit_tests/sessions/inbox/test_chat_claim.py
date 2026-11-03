import pytest
from sessions.inbox import kinds
from sessions.inbox.chat_claim import ChatClaim
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session

pytestmark = pytest.mark.django_db(transaction=True)


async def _state(event: InboxEvent) -> InboxEvent:
    return await InboxEvent.objects.aget(pk=event.pk)


async def _queued_next_run(session: Session, *, ref: str | None = None, trigger=SessionOrigin.PIPELINE_WEBHOOK) -> Run:
    return await Run.objects.acreate(
        session=session, trigger_type=trigger, repo_id=session.repo_id, ref=session.ref if ref is None else ref,
        status=RunStatus.QUEUED,
    )  # fmt: skip


class TestTake:
    async def test_a_chat_turn_supersedes_an_older_failure_with_the_pass_it_takes(self):
        session = await amake_session()
        failure = await amake_event(session, merge_request_iid=9, subject=kinds.Subject.ci(9))
        passed = await amake_event(session, EventKind.CI_PASSED, merge_request_iid=9, subject=kinds.Subject.ci(9))

        taken = await ChatClaim(session.thread_id, "chat-run").atake()

        assert [event.pk for event in taken] == [passed.pk]
        assert (await _state(failure)).state == EventState.SUPERSEDED

    async def test_it_drops_a_next_run_its_supersede_leaves_with_nothing_to_wake_it(self):
        session = await amake_session()
        emptied = await _queued_next_run(session)
        await amake_event(session, merge_request_iid=9, subject=kinds.Subject.ci(9), run=emptied)
        kept = await _queued_next_run(session, ref="feat/y", trigger=SessionOrigin.API_JOB)
        await amake_event(session, EventKind.FOLLOW_UP_JOB, ref="feat/y", run=kept)
        await amake_event(session, EventKind.CI_PASSED, merge_request_iid=9, subject=kinds.Subject.ci(9))

        await ChatClaim(session.thread_id, "chat-run").atake()

        assert not await Run.objects.filter(pk=emptied.pk).aexists()
        assert await Run.objects.filter(pk=kept.pk).aexists()


class TestRelease:
    async def test_the_events_a_turn_took_and_no_run_took_over_go_back_to_pending(self):
        session = await amake_session()
        claimed = await amake_event(session, EventKind.MR_MERGED, state=EventState.TAKEN, claim_token="r-1")  # noqa: S106
        run = await Run.objects.acreate(session=session, trigger_type=SessionOrigin.CHAT, repo_id=session.repo_id)
        linked = await amake_event(session, EventKind.MR_MERGED, state=EventState.TAKEN, run=run)
        other_holder = await amake_event(session, EventKind.MR_MERGED, state=EventState.TAKEN, claim_token="r-2")  # noqa: S106
        elsewhere = await amake_event(
            await amake_session(),
            EventKind.MR_MERGED,
            state=EventState.TAKEN,
            claim_token="r-1",  # noqa: S106
        )

        assert await ChatClaim(session.thread_id, "r-1").arelease() == 1

        stored = await _state(claimed)
        assert (stored.state, stored.claim_token, stored.attempts) == (EventState.PENDING, "", 0)
        assert [(await _state(event)).state for event in (linked, other_holder, elsewhere)] == [EventState.TAKEN] * 3
