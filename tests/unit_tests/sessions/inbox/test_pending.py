import pytest
from asgiref.sync import sync_to_async
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.pending import PendingEvents
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session

pytestmark = pytest.mark.django_db(transaction=True)


class TestBranchOf:
    async def test_a_request_that_follows_the_session_takes_its_branch(self):
        session = await amake_session(ref="feat/new")
        event = await amake_event(session, EventKind.CHAT_MESSAGE, ref="feat/old")

        assert PendingEvents(session, [event]).branch_of(event) == "feat/new"

    async def test_any_other_event_keeps_the_branch_it_was_posted_on(self):
        session = await amake_session(ref="feat/new")
        event = await amake_event(session, EventKind.CI_FAILED, ref="feat/old")

        assert PendingEvents(session, [event]).branch_of(event) == "feat/old"


class TestNewestOfEachSubject:
    async def test_a_newer_event_of_a_subject_replaces_the_older(self):
        session = await amake_session()
        older = await amake_event(session, EventKind.CI_FAILED, subject="ci:1")
        newer = await amake_event(session, EventKind.CI_PASSED, subject="ci:1")
        other = await amake_event(session, EventKind.CHAT_MESSAGE)

        assert PendingEvents(session, [older, newer, other]).newest_of_each_subject() == [newer, other]


class TestSupersede:
    async def test_it_marks_the_replaced_events_superseded_and_returns_the_rest(self):
        session = await amake_session()
        older = await amake_event(session, EventKind.CI_FAILED, subject="ci:1")
        newer = await amake_event(session, EventKind.CI_PASSED, subject="ci:1")

        survivors = await sync_to_async(PendingEvents(session, [older, newer]).supersede)()

        assert survivors == [newer]
        assert (await InboxEvent.objects.aget(pk=older.pk)).state == EventState.SUPERSEDED
        assert (await InboxEvent.objects.aget(pk=newer.pk)).state == EventState.PENDING


class TestFollowSessionBranch:
    async def _queued(self, session, ref):
        return await Run.objects.acreate(
            session=session, repo_id=session.repo_id, ref=ref, status=RunStatus.QUEUED,
            trigger_type=SessionOrigin.CHAT,
        )  # fmt: skip

    async def test_a_next_run_only_such_requests_wait_in_moves_with_them(self):
        session = await amake_session(ref="feat/new")
        run = await self._queued(session, "feat/old")
        event = await amake_event(session, EventKind.CHAT_MESSAGE, ref="feat/old", run=run)

        await sync_to_async(PendingEvents(session, [event]).follow_session_branch)([run])

        assert (await InboxEvent.objects.aget(pk=event.pk)).ref == "feat/new"
        assert (await InboxEvent.objects.aget(pk=event.pk)).run_id == run.pk
        assert (await Run.objects.aget(pk=run.pk)).ref == "feat/new"

    async def test_a_next_run_that_holds_other_requests_lets_them_go(self):
        session = await amake_session(ref="feat/new")
        run = await self._queued(session, "feat/old")
        follower = await amake_event(session, EventKind.CHAT_MESSAGE, ref="feat/old", run=run)
        stays = await amake_event(session, EventKind.CI_FAILED, ref="feat/old", run=run)

        await sync_to_async(PendingEvents(session, [follower, stays]).follow_session_branch)([run])

        assert (await InboxEvent.objects.aget(pk=follower.pk)).run_id is None
        assert (await InboxEvent.objects.aget(pk=stays.pk)).run_id == run.pk
        assert (await Run.objects.aget(pk=run.pk)).ref == "feat/old"

    async def test_nothing_moves_when_every_request_is_on_the_sessions_branch(self):
        session = await amake_session(ref="feat/new")
        run = await self._queued(session, "feat/new")
        event = await amake_event(session, EventKind.CHAT_MESSAGE, ref="feat/new", run=run)

        await sync_to_async(PendingEvents(session, [event]).follow_session_branch)([run])

        assert (await InboxEvent.objects.aget(pk=event.pk)).run_id == run.pk
        assert await sync_to_async(NextRuns(session).queued().count)() == 1
