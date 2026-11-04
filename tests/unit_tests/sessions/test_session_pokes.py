"""Every write that changes what a session's page shows must poke that page.

A missed poke is invisible: the page keeps showing the old state until something else wakes it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

from django.test import TestCase
from django.utils import timezone

import pytest
from ninja.testing import TestAsyncClient
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.inbox import kinds
from sessions.inbox.chat_claim import ChatClaim
from sessions.inbox.complete import RunEnd
from sessions.inbox.drain import Drain
from sessions.inbox.ownership import Ownerships
from sessions.inbox.post import Inbox
from sessions.models import (
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.pipeline_watch.store import WatchStore
from sessions.queue import EnqueueFailure, dispatch_next_queued
from sessions.services import RepoTarget, asubmit_batch_runs
from sessions.tasks import generate_batch_title_task, generate_title_task

from accounts.models import APIKey, User
from codebase.authorization import RepositoryAccessDenied
from daiv.api import api
from tests.unit_tests.sessions.conftest import (
    active_holder,
    amake_job_session,
    amake_owned_merge_request,
    make_coordinator,
    make_leg,
)
from tests.unit_tests.sessions.inbox.conftest import amake_event, amake_session, ci_payload, make_event

PUBLISH = "core.ui_events.publisher.session_changed"
HOLDER = "chat-run"


@pytest.fixture
def publish():
    with patch(PUBLISH) as mock:
        yield mock


@pytest.fixture
def session(db):
    return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api")


def make_run(session: Session, status: str = RunStatus.QUEUED) -> Run:
    return Run.objects.create(session=session, status=status, trigger_type=SessionOrigin.UI_JOB, repo_id="daiv/api")


@pytest.mark.django_db
class TestRunWritesPoke:
    def test_creating_a_queued_run_pokes(self, session, publish):
        with TestCase.captureOnCommitCallbacks(execute=True):
            make_run(session, status=RunStatus.QUEUED)

        publish.assert_any_call(session.thread_id)

    def test_a_status_save_pokes(self, session, publish):
        run = make_run(session)

        with TestCase.captureOnCommitCallbacks(execute=True):
            run.status = RunStatus.RUNNING
            run.save(update_fields=["status"])

        publish.assert_any_call(session.thread_id)

    def test_a_save_without_status_does_not_poke(self, session, publish):
        run = make_run(session, status=RunStatus.RUNNING)

        with TestCase.captureOnCommitCallbacks(execute=True):
            run.result_summary = "done"
            run.save(update_fields=["result_summary"])

        publish.assert_not_called()

    def test_deleting_a_run_pokes(self, session, publish):
        run = make_run(session)

        with TestCase.captureOnCommitCallbacks(execute=True):
            run.delete()

        publish.assert_any_call(session.thread_id)

    def test_finishing_a_run_pokes(self, session, publish):
        run = make_run(session, status=RunStatus.RUNNING)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.finish(run, RunStatus.SUCCESSFUL) is True

        publish.assert_any_call(session.thread_id)

    def test_a_sync_status_update_pokes(self, session, publish):
        run = make_run(session, status=RunStatus.READY)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.update_unfinished(run, status=RunStatus.RUNNING) is True

        publish.assert_any_call(session.thread_id)


@pytest.mark.django_db(transaction=True)
class TestAsyncRunWritesPoke:
    async def test_afinish_pokes(self, publish):
        thread_id = await amake_job_session()
        session = await Session.objects.aget(pk=thread_id)
        run = await Run.objects.acreate(
            session=session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.UI_JOB, repo_id="owner/repo"
        )
        publish.reset_mock()

        assert await Run.objects.afinish(run, RunStatus.SUCCESSFUL) is True

        publish.assert_any_call(thread_id)


@pytest.mark.django_db(transaction=True)
class TestSlotChangesPoke:
    async def test_claiming_the_slot_pokes(self, publish):
        thread_id = await amake_job_session()

        assert await SessionLock.try_claim(thread_id, "run-1") is True

        publish.assert_any_call(thread_id)

    async def test_a_stale_takeover_pokes(self, publish):
        stale = stale_cutoff() - timedelta(minutes=1)
        thread_id = await amake_job_session(active_run_id="run-1", last_active_at=stale)

        assert await SessionLock.try_claim(thread_id, "run-2") is True

        publish.assert_any_call(thread_id)

    async def test_a_failed_claim_does_not_poke(self, publish):
        thread_id = await amake_job_session(active_run_id="run-1")

        assert await SessionLock.try_claim(thread_id, "run-2") is False

        publish.assert_not_called()

    async def test_releasing_the_slot_pokes(self, publish):
        thread_id = await amake_job_session(active_run_id="run-1")

        await SessionLock.release(thread_id, "run-1")

        assert await active_holder(thread_id) is None
        publish.assert_any_call(thread_id)

    async def test_releasing_a_slot_held_by_someone_else_does_not_poke(self, publish):
        thread_id = await amake_job_session(active_run_id="run-2")

        await SessionLock.release(thread_id, "run-1")

        assert await active_holder(thread_id) == "run-2"
        publish.assert_not_called()

    async def test_a_heartbeat_does_not_poke(self, publish):
        thread_id = await amake_job_session(active_run_id="run-1")

        assert await SessionLock.heartbeat(thread_id, "run-1") is True

        publish.assert_not_called()


@pytest.mark.django_db
class TestReleaseForRun:
    def test_releasing_a_runs_slot_pokes(self, session, publish):
        Session.objects.filter(pk=session.pk).update(active_run_id="run-1")

        with TestCase.captureOnCommitCallbacks(execute=True):
            SessionLock.release_for_run(session.thread_id, "run-1")

        publish.assert_any_call(session.thread_id)

    def test_releasing_a_slot_the_run_does_not_hold_does_not_poke(self, session, publish):
        Session.objects.filter(pk=session.pk).update(active_run_id="run-2")

        with TestCase.captureOnCommitCallbacks(execute=True):
            SessionLock.release_for_run(session.thread_id, "run-1")

        publish.assert_not_called()


@pytest.mark.django_db
class TestCancelPokes:
    def test_asking_a_running_delegated_run_to_stop_pokes_its_coordinator(self, publish):
        coordinator = make_coordinator()
        leg = make_leg(coordinator, uuid.uuid4(), status=RunStatus.RUNNING, finished_at=None)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.cancel(leg) is True

        assert Run.objects.get(pk=leg.pk).status == RunStatus.RUNNING
        publish.assert_any_call(leg.session_id)
        publish.assert_any_call(coordinator.thread_id)

    def test_a_run_already_asked_to_stop_does_not_poke(self, publish):
        coordinator = make_coordinator()
        leg = make_leg(
            coordinator, uuid.uuid4(), status=RunStatus.RUNNING, finished_at=None, cancel_requested_at=timezone.now()
        )

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Run.objects.cancel(leg) is False

        publish.assert_not_called()


@pytest.fixture
def enqueue():
    with patch("sessions.inbox.drain.aenqueue_run", new=AsyncMock(return_value=None)) as enqueue:
        yield enqueue


def make_inbox_session(**fields) -> Session:
    """SYNC ONLY: the session :func:`amake_session` makes."""
    defaults = {"origin": SessionOrigin.CHAT, "repo_id": "group/repo", "ref": "chore/x"}
    return Session.objects.create(thread_id=str(uuid.uuid4()), **(defaults | fields))


@pytest.mark.django_db
class TestInboxWritesPoke:
    def test_posting_an_event_pokes(self, session, publish):
        with TestCase.captureOnCommitCallbacks(execute=True):
            result = Inbox(session.thread_id).post(
                kind=EventKind.NOTICE, source_key="notice:1", payload={"text": "Something happened."}, wake=False
            )

        assert result.created is True
        publish.assert_any_call(session.thread_id)

    def test_posting_the_same_source_again_does_not_poke(self, session, publish):
        inbox = Inbox(session.thread_id)
        notice = {"kind": EventKind.NOTICE, "source_key": "notice:1", "payload": {"text": "Hi."}, "wake": False}
        inbox.post(**notice)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert inbox.post(**notice).created is False

        publish.assert_not_called()

    def test_posting_a_delegated_ci_outcome_pokes_the_parent(self, session, publish):
        child = make_inbox_session(parent=session)

        with TestCase.captureOnCommitCallbacks(execute=True):
            Inbox.post_delegated_ci_outcome(
                child, merge_request_iid=9, pipeline_id=1, outcome="passing", web_url="https://ci/1"
            )

        publish.assert_any_call(session.thread_id)

    def test_posting_no_push_pokes_the_parent(self, session, publish):
        child = make_inbox_session(parent=session)
        failure = make_event(child, merge_request_iid=9)

        with TestCase.captureOnCommitCallbacks(execute=True):
            Inbox.post_no_push(child, failure)

        publish.assert_any_call(session.thread_id)

    def test_attaching_the_next_run_pokes(self, publish):
        session = make_inbox_session(active_run_id="chat-run")
        next_run = Run.objects.create(
            session=session,
            trigger_type=SessionOrigin.PIPELINE_WEBHOOK,
            repo_id=session.repo_id,
            ref=session.ref,
            status=RunStatus.QUEUED,
        )
        make_event(session, run=next_run)
        event = make_event(session, payload=ci_payload(2))

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Inbox(session.thread_id).attach_next_run(event) == next_run

        publish.assert_any_call(session.thread_id)

    def test_a_chat_claim_that_takes_events_pokes(self, session, publish):
        make_event(session, EventKind.NOTICE)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert len(ChatClaim(session.thread_id, HOLDER).take()) == 1

        publish.assert_any_call(session.thread_id)

    def test_a_chat_claim_with_nothing_to_take_does_not_poke(self, session, publish):
        with TestCase.captureOnCommitCallbacks(execute=True):
            assert ChatClaim(session.thread_id, HOLDER).take() == []

        publish.assert_not_called()

    def test_settling_a_runs_events_pokes(self, session, publish):
        run = make_run(session, status=RunStatus.SUCCESSFUL)
        make_event(session, EventKind.NOTICE, state=EventState.TAKEN, run=run)

        with TestCase.captureOnCommitCallbacks(execute=True):
            RunEnd(run).settle_events()

        publish.assert_any_call(session.thread_id)

    def test_settling_a_run_that_took_nothing_does_not_poke(self, session, publish):
        run = make_run(session, status=RunStatus.SUCCESSFUL)

        with TestCase.captureOnCommitCallbacks(execute=True):
            RunEnd(run).settle_events()

        publish.assert_not_called()

    def test_recovering_stale_events_pokes_each_session(self, publish):
        stale = stale_cutoff() - timedelta(minutes=1)
        orphaned, ended = (make_inbox_session(active_run_id="gone", last_active_at=stale) for _ in range(2))
        make_event(orphaned, EventKind.NOTICE, state=EventState.TAKEN)
        make_event(ended, EventKind.NOTICE, state=EventState.TAKEN, run=make_run(ended, status=RunStatus.FAILED))

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert RunEnd.recover_stale_events() == 2

        publish.assert_any_call(orphaned.thread_id)
        publish.assert_any_call(ended.thread_id)


@pytest.mark.django_db
class TestDrainPokes:
    def test_a_drain_that_takes_a_run_pokes(self, enqueue, publish):
        session = make_inbox_session()
        make_event(session)

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Drain(session.thread_id).start() is not None

        publish.assert_any_call(session.thread_id)

    def test_a_drain_with_nothing_to_take_does_not_poke(self, enqueue, publish):
        session = make_inbox_session()

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Drain(session.thread_id).start() is None

        publish.assert_not_called()

    def test_a_drain_that_supersedes_without_taking_a_run_pokes(self, enqueue, publish):
        session = make_inbox_session()
        older, _newer = (
            make_event(session, EventKind.CI_PASSED, subject=kinds.Subject.ci(9), merge_request_iid=9) for _ in range(2)
        )

        with TestCase.captureOnCommitCallbacks(execute=True):
            assert Drain(session.thread_id).start() is None

        assert InboxEvent.objects.get(pk=older.pk).state == EventState.SUPERSEDED
        publish.assert_any_call(session.thread_id)

    def test_a_drain_that_fails_a_denied_requesters_events_pokes(self, enqueue, publish, member_user):
        session = make_inbox_session(user=member_user)
        event = make_event(session)
        denied = Mock(side_effect=RepositoryAccessDenied([session.repo_id]))

        with patch("sessions.admission.assert_can_run", denied), TestCase.captureOnCommitCallbacks(execute=True):
            assert Drain(session.thread_id).start() is None

        assert InboxEvent.objects.get(pk=event.pk).state == EventState.FAILED
        publish.assert_any_call(session.thread_id)


@pytest.mark.django_db(transaction=True)
class TestAsyncInboxWritesPoke:
    async def test_linking_a_chat_claim_pokes(self, publish):
        session = await amake_session()
        run = await Run.objects.acreate(
            session=session, trigger_type=SessionOrigin.CHAT, repo_id=session.repo_id, status=RunStatus.RUNNING
        )
        await amake_event(session, EventKind.NOTICE, state=EventState.TAKEN, claim_token=HOLDER)
        publish.reset_mock()

        await ChatClaim(session.thread_id, HOLDER).alink(run)

        publish.assert_any_call(session.thread_id)

    async def test_releasing_a_chat_claim_pokes(self, publish):
        session = await amake_session()
        await amake_event(session, EventKind.NOTICE, state=EventState.TAKEN, claim_token=HOLDER)

        assert await ChatClaim(session.thread_id, HOLDER).arelease() == 1

        publish.assert_any_call(session.thread_id)

    async def test_releasing_a_chat_claim_that_took_nothing_does_not_poke(self, publish):
        session = await amake_session()

        assert await ChatClaim(session.thread_id, HOLDER).arelease() == 0

        publish.assert_not_called()

    async def test_a_drain_whose_enqueue_failed_pokes_once_it_refunded_the_attempt(self, publish):
        session = await amake_session()
        ownership = await MergeRequestOwnership.objects.acreate(
            repo_id=session.repo_id, merge_request_iid=9, session=session, source_branch=session.ref
        )
        await amake_event(session, merge_request_iid=9, payload=ci_payload(sha="e96f1f77"))

        async def fail_once_the_run_ended(run, **kwargs):
            await Session.objects.filter(pk=run.session_id).aupdate(active_run_id=None)
            await InboxEvent.objects.filter(run=run).aupdate(state=EventState.PENDING, run=None)
            publish.reset_mock()
            return EnqueueFailure(task_enqueued=False)

        with patch("sessions.inbox.drain.aenqueue_run", new=fail_once_the_run_ended):
            assert await Drain(session.thread_id).astart() is None

        await ownership.arefresh_from_db()
        assert ownership.attempts == 0
        publish.assert_any_call(session.thread_id)

    async def test_dismissing_a_follow_up_job_that_did_not_start_pokes(self, publish, member_user):
        thread_id = await amake_job_session()

        def no_next_run(event):
            publish.reset_mock()

        with (
            patch.object(Drain, "start_logged", autospec=True),
            patch.object(Inbox, "aattach_next_run", new=AsyncMock(side_effect=no_next_run)),
        ):
            result = await asubmit_batch_runs(
                user=member_user,
                prompt="follow-up",
                repos=[RepoTarget(repo_id="owner/repo", ref="")],
                trigger_type=SessionOrigin.API_JOB,
                thread_id=thread_id,
            )

        assert result.runs == []
        assert (await InboxEvent.objects.aget(session_id=thread_id)).state == EventState.DISMISSED
        publish.assert_any_call(thread_id)


REPO = "group/repo"
MR = {"merge_request_id": 9, "source_branch": "chore/install-gettext", "title": "Install gettext"}


async def _publishing_run(user: User | None) -> Run:
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=REPO, ref=MR["source_branch"], user=user
    )
    return await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.CHAT, repo_id=REPO, status=RunStatus.RUNNING, user=user
    )


async def _claim(run: Run, sha: str = "e96f1f77") -> MergeRequestOwnership:
    return await Ownerships.aclaim(run_id=run.pk, merge_request=MR, pushed_sha=sha)


@pytest.mark.django_db(transaction=True)
class TestOwnershipChangesPoke:
    @staticmethod
    async def _alice() -> User:
        return await User.objects.acreate(username="alice", email="alice@example.com")

    async def test_claiming_a_merge_request_pokes_the_owning_session(self, publish):
        run = await _publishing_run(await self._alice())
        publish.reset_mock()

        await _claim(run)

        publish.assert_any_call(run.session_id)

    async def test_moving_a_merge_request_pokes_the_session_it_left(self, publish):
        alice = await self._alice()
        first, second = await _publishing_run(alice), await _publishing_run(alice)
        await _claim(first)
        publish.reset_mock()

        assert (await _claim(second, sha="0a1b2c3d")).session_id == second.session_id

        publish.assert_any_call(first.session_id)
        publish.assert_any_call(second.session_id)

    async def test_closing_a_merge_request_pokes_the_owning_session(self, publish):
        run = await _publishing_run(await self._alice())
        await _claim(run)
        publish.reset_mock()

        assert await Ownerships(REPO).aclose(9, merged=True, by_username="sfr", target_branch="main") is True

        publish.assert_any_call(run.session_id)

    async def test_reopening_a_merge_request_pokes_the_owning_session(self, publish):
        run = await _publishing_run(await self._alice())
        await _claim(run)
        await Ownerships(REPO).aclose(9, merged=False, by_username="sfr", target_branch="main")
        publish.reset_mock()

        assert await Ownerships(REPO).areopen(9, by_username="sfr") is True

        publish.assert_any_call(run.session_id)


@pytest.mark.django_db(transaction=True)
class TestWatchChangesPoke:
    async def test_recording_a_pipeline_pokes_the_owning_session(self, publish):
        ownership = await amake_owned_merge_request()

        assert await WatchStore().arecord_pipeline(ownership.pk, 5, "failed") is True

        publish.assert_any_call(ownership.session_id)

    async def test_recording_a_pipeline_already_judged_does_not_poke(self, publish):
        ownership = await amake_owned_merge_request(last_pipeline_id=5, last_pipeline_status="failed")

        assert await WatchStore().arecord_pipeline(ownership.pk, 5, "failed") is False

        publish.assert_not_called()

    async def test_forgetting_a_pipeline_pokes_the_owning_session(self, publish):
        ownership = await amake_owned_merge_request(last_pipeline_id=5, last_pipeline_status="failed")

        await WatchStore().aforget_pipeline(ownership.pk, 5, "failed", previous=(None, ""))

        publish.assert_any_call(ownership.session_id)

    async def test_turning_the_watch_off_pokes_the_owning_session(self, publish):
        ownership = await amake_owned_merge_request()

        await WatchStore().aset_enabled(ownership.pk, enabled=False)

        publish.assert_any_call(ownership.session_id)


@pytest.mark.django_db(transaction=True)
class TestQueuePokes:
    def test_promoting_a_queued_run_pokes(self, session, publish, create_db_task_result):
        run = make_run(session, status=RunStatus.QUEUED)
        publish.reset_mock()

        with patch("sessions.queue.run_job_task") as task:
            task.aenqueue = AsyncMock(return_value=MagicMock(id=create_db_task_result().id))
            dispatch_next_queued(session.thread_id)

        run.refresh_from_db()
        assert run.status == RunStatus.READY
        publish.assert_any_call(session.thread_id)


def _run_for_titling(*, batch_id: uuid.UUID, session_title: str = "") -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id=REPO, title=session_title
    )
    return Run.objects.create(session=session, trigger_type=SessionOrigin.API_JOB, repo_id=REPO, batch_id=batch_id)


@pytest.mark.django_db
class TestTitleChangesPoke:
    def test_auto_titling_pokes(self, publish):
        session = make_inbox_session(title="add login")

        with (
            patch("sessions.titling.generate_title", return_value="LLM generated"),
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            generate_title_task.func(pk=session.thread_id, prompt="add login", repo_id=REPO)

        publish.assert_any_call(session.thread_id)

    def test_auto_titling_a_renamed_session_does_not_poke(self, publish):
        session = make_inbox_session(title="My rename")

        with (
            patch("sessions.titling.generate_title", return_value="LLM generated"),
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            generate_title_task.func(pk=session.thread_id, prompt="add login", repo_id=REPO)

        publish.assert_not_called()

    def test_batch_auto_titling_pokes_each_session_it_titled(self, publish):
        batch_id = uuid.uuid4()
        untitled = [_run_for_titling(batch_id=batch_id) for _ in range(2)]
        pinned = _run_for_titling(batch_id=batch_id, session_title="Session pinned")

        with (
            patch("sessions.titling.generate_title", return_value="Batch title"),
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

        for run in untitled:
            publish.assert_any_call(run.session_id)
        assert call(pinned.session_id) not in publish.call_args_list


@pytest.mark.django_db(transaction=True)
async def test_renaming_pokes(publish):
    user = await User.objects.acreate_user(username="sessuser", email="sessuser@example.com", password="x")  # noqa: S106
    _, key = await APIKey.objects.create_key(user=user, name="Test")
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=REPO, ref="main", user=user, title="Old"
    )

    response = await TestAsyncClient(api).patch(
        f"/sessions/{session.thread_id}", json={"title": "New"}, headers={"Authorization": f"Bearer {key}"}
    )

    assert response.status_code == 200
    publish.assert_any_call(session.thread_id)
