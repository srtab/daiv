"""Every Run write that moves the running-runs badge must poke the nav readers.

The badge is only as live as its pokes, and the ways a Run's status changes are not
uniform: most go through ``save()`` (and so through ``post_save``), but the chat
lifecycle and the stuck-run reaper write with ``aupdate``/``update``, which fire no
signal at all. These tests cover both kinds, because a missed poke is invisible —
the badge just sits on a stale number until the stream's next reconnect.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

import pytest
from sessions.locks import stale_cutoff
from sessions.models import Run, RunStatus, Session, SessionOrigin


@pytest.fixture
def session(db):
    return Session.objects.create(thread_id=str(uuid.uuid4()), origin=SessionOrigin.UI_JOB, repo_id="daiv/api")


def make_run(session, status=RunStatus.QUEUED, trigger_type=SessionOrigin.UI_JOB) -> Run:
    return Run.objects.create(session=session, status=status, trigger_type=trigger_type, repo_id="daiv/api")


@pytest.mark.django_db
class TestRunSavePokes:
    def test_a_status_change_pokes(self, session):
        run = make_run(session)
        with patch("sessions.signals.publish_runs_changed") as publish, TestCase.captureOnCommitCallbacks(execute=True):
            run.status = RunStatus.RUNNING
            run.save(update_fields=["status"])
        publish.assert_called_once()

    def test_creating_a_run_pokes(self, session):
        with patch("sessions.signals.publish_runs_changed") as publish, TestCase.captureOnCommitCallbacks(execute=True):
            make_run(session, status=RunStatus.RUNNING)
        publish.assert_called_once()

    def test_a_save_that_cannot_have_moved_the_status_does_not_poke(self, session):
        run = make_run(session, status=RunStatus.RUNNING)
        with patch("sessions.signals.publish_runs_changed") as publish, TestCase.captureOnCommitCallbacks(execute=True):
            run.result_summary = "done"
            run.save(update_fields=["result_summary"])
        publish.assert_not_called()

    def test_the_poke_waits_for_the_commit(self, session):
        """A reader recounting before the write landed would send the old number and
        then sit on it — nothing pokes twice."""
        run = make_run(session)
        with patch("sessions.signals.publish_runs_changed") as publish:
            # `captureOnCommitCallbacks` only fills the list on exit, so the deferred
            # callbacks have to run outside it — the patch stays up around both.
            with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
                run.status = RunStatus.RUNNING
                run.save(update_fields=["status"])
            publish.assert_not_called()
            for callback in callbacks:
                callback()
            publish.assert_called_once()


@pytest.mark.django_db(transaction=True)
class TestWritesThatBypassSignals:
    async def test_finalizing_a_chat_run_pokes(self, session):
        """``finalize_chat_run`` uses ``aupdate``, so ``post_save`` never fires — and this
        is the transition that takes the badge back down after a chat turn."""
        from chat.api.streaming import finalize_chat_run

        run = await Run.objects.acreate(
            session=session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.CHAT, repo_id="daiv/api"
        )
        with patch("chat.api.streaming.apublish_runs_changed") as publish:
            await finalize_chat_run(run.pk, success=True, usage=None, response_text="ok")
        publish.assert_called_once()

    def test_reaping_orphaned_chat_runs_pokes(self, session):
        """The reaper's bulk ``.update()`` fires no signal either, and it is what clears a
        badge left counting a run whose worker was killed."""
        Session.objects.filter(pk=session.pk).update(last_active_at=stale_cutoff() - timedelta(minutes=5))
        make_run(session, status=RunStatus.RUNNING, trigger_type=SessionOrigin.CHAT)
        with patch("sessions.management.commands.sync_stuck_runs.publish_runs_changed") as publish:
            call_command("sync_stuck_runs")
        publish.assert_called_once()

    def test_a_reaper_pass_with_nothing_to_reap_does_not_poke(self, session):
        with patch("sessions.management.commands.sync_stuck_runs.publish_runs_changed") as publish:
            call_command("sync_stuck_runs")
        publish.assert_not_called()
