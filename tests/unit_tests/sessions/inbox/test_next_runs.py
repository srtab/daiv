from datetime import timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sessions.executor.lock import stale_cutoff
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.requester import Requester
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, SessionOrigin

from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session

pytestmark = pytest.mark.django_db(transaction=True)


async def _queued(session, *, ref=None, trigger=SessionOrigin.PIPELINE_WEBHOOK, **fields) -> Run:
    return await Run.objects.acreate(
        session=session, trigger_type=trigger, repo_id=session.repo_id, ref=session.ref if ref is None else ref,
        **{"status": RunStatus.QUEUED} | fields,
    )  # fmt: skip


class TestQueued:
    async def test_it_lists_only_queued_runs_that_have_events_waiting(self):
        session = await amake_session()
        waiting = await _queued(session)
        await amake_event(session, run=waiting)
        await _queued(session, ref="feat/empty")
        await Run.objects.acreate(
            session=session, repo_id=session.repo_id, ref="x", status=RunStatus.READY,
            trigger_type=SessionOrigin.PIPELINE_WEBHOOK,
        )  # fmt: skip

        runs = await sync_to_async(list)(NextRuns(session).queued())

        assert runs == [waiting]


class TestForBranch:
    async def test_it_finds_the_run_of_that_branch_and_requester(self, admin_user, member_user):
        session = await amake_session()
        mine = await _queued(session, user=admin_user)
        theirs = await _queued(session, ref="feat/other", user=member_user)
        await amake_event(session, run=mine)
        await amake_event(session, run=theirs)
        next_runs = NextRuns(session)

        assert await sync_to_async(next_runs.for_branch)(session.ref, Requester(admin_user.pk)) == mine
        assert await sync_to_async(next_runs.for_branch)(session.ref, Requester(member_user.pk)) is None


class TestIsBusy:
    async def test_a_live_holder_makes_the_session_busy(self):
        session = await amake_session(active_run_id="chat-run", last_active_at=timezone.now())

        assert await NextRuns(session).ais_busy()

    async def test_a_stale_holder_does_not(self):
        session = await amake_session(active_run_id="gone", last_active_at=stale_cutoff() - timedelta(minutes=1))

        assert not await NextRuns(session).ais_busy()

    async def test_a_queued_next_run_does(self):
        session = await amake_session()
        await amake_event(session, run=await _queued(session))

        assert await NextRuns(session).ais_busy()


class TestJobRunActive:
    @pytest.mark.parametrize(
        ("status", "active"),
        [(RunStatus.READY, True), (RunStatus.RUNNING, True), (RunStatus.QUEUED, False), (RunStatus.SUCCESSFUL, False)],
    )
    async def test_only_a_job_run_holding_the_active_place_counts(self, status, active):
        session = await amake_session()
        await _queued(session, trigger=SessionOrigin.API_JOB, status=status)

        assert await sync_to_async(NextRuns(session).job_run_active)() is active


class TestJoin:
    async def test_an_idle_session_gives_the_event_no_run(self):
        session = await amake_session()
        event = await amake_event(session, EventKind.CHAT_MESSAGE)

        assert await sync_to_async(NextRuns(session).join)(event) is None

    async def test_a_busy_session_creates_the_next_run_and_describes_it(self):
        session = await amake_session(active_run_id="chat-run", last_active_at=timezone.now())
        event = await amake_event(session, EventKind.CHAT_MESSAGE)

        run = await sync_to_async(NextRuns(session).join)(event)

        assert run is not None
        assert run.status == RunStatus.QUEUED
        assert (await InboxEvent.objects.aget(pk=event.pk)).run_id == run.pk
        assert (await Run.objects.aget(pk=run.pk)).prompt

    async def test_a_second_event_for_the_branch_joins_the_same_run(self):
        session = await amake_session(active_run_id="chat-run", last_active_at=timezone.now())
        first = await amake_event(session, EventKind.CHAT_MESSAGE)
        second = await amake_event(session, EventKind.CHAT_MESSAGE)
        next_runs = NextRuns(session)

        run = await sync_to_async(next_runs.join)(first)

        assert await sync_to_async(next_runs.join)(second) == run

    async def test_a_run_holding_the_places_leaves_the_event_waiting_and_logs(self, caplog):
        session = await amake_session(active_run_id="chat-run", last_active_at=timezone.now())
        event = await amake_event(session, EventKind.CHAT_MESSAGE)

        with (
            caplog.at_level("INFO", logger="daiv.sessions"),
            patch.object(Run.objects, "create", side_effect=IntegrityError),
        ):
            run = await sync_to_async(NextRuns(session).join)(event)

        assert run is None
        assert (await InboxEvent.objects.aget(pk=event.pk)).run_id is None
        assert "an older queued run holds" in caplog.text


class TestMove:
    async def test_it_moves_the_run_to_the_branch(self):
        session = await amake_session()
        run = await _queued(session, ref="feat/old")

        assert await sync_to_async(NextRuns(session).move)(run, "feat/new") is True
        assert run.ref == "feat/new"
        assert (await Run.objects.aget(pk=run.pk)).ref == "feat/new"

    async def test_it_refuses_when_a_queued_run_holds_the_place(self):
        session = await amake_session()
        await _queued(session, ref="feat/new")
        run = await _queued(session, ref="feat/old")

        assert await sync_to_async(NextRuns(session).move)(run, "feat/new") is False
        assert run.ref == "feat/old"


class TestDrop:
    async def test_it_deletes_the_runs_and_detaches_their_events(self):
        session = await amake_session()
        run = await _queued(session)
        event = await amake_event(session, run=run)

        await sync_to_async(NextRuns(session).drop)([run, None])

        assert not await Run.objects.filter(pk=run.pk).aexists()
        assert (await InboxEvent.objects.aget(pk=event.pk)).run_id is None
        assert (await InboxEvent.objects.aget(pk=event.pk)).state == EventState.PENDING


class TestDropEmptied:
    async def test_it_deletes_only_the_runs_left_with_no_event(self):
        session = await amake_session()
        emptied = await _queued(session, ref="feat/a")
        kept = await _queued(session, ref="feat/b")
        await amake_event(session, run=kept)

        await sync_to_async(NextRuns(session).drop_emptied)([emptied, kept])

        assert not await Run.objects.filter(pk=emptied.pk).aexists()
        assert await Run.objects.filter(pk=kept.pk).aexists()
