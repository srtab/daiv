"""Every write that changes what a session's page shows must poke that page.

A missed poke is invisible: the page keeps showing the old state until something else wakes it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase

import pytest
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.models import Run, RunStatus, Session, SessionOrigin

from tests.unit_tests.sessions.conftest import active_holder, amake_job_session

PUBLISH = "core.ui_events.publisher.session_changed"


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
