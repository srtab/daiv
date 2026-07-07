"""Tests for notifications receivers wired to sessions.signals.run_finished."""

from unittest.mock import patch

import pytest
from notifications.choices import NotifyOn
from notifications.models import Notification
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.signals import run_finished

from schedules.models import Frequency, ScheduledJob


def _session(*, origin=SessionOrigin.API_JOB, thread_id="thread-run-1", repo_id="x/y", **kwargs):
    return Session.objects.create(thread_id=thread_id, origin=origin, repo_id=repo_id, **kwargs)


def _run(session, *, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL, repo_id="x/y", **kwargs):
    return Run.objects.create(session=session, trigger_type=trigger_type, status=status, repo_id=repo_id, **kwargs)


@pytest.mark.django_db
class TestMemorySkipChatRuns:
    """capture_run_observations must ignore CHAT-triggered runs."""

    def test_memory_skips_chat_runs(self):
        session = _session(origin=SessionOrigin.CHAT, thread_id="chat-thread")
        run = _run(session, trigger_type=SessionOrigin.CHAT)
        with patch("memory.signals.extract_observations_task") as task_mock:
            run_finished.send(sender=Run, run=run)
        task_mock.enqueue.assert_not_called()

    def test_memory_processes_api_job_runs(self):
        session = _session()
        run = _run(session)
        with patch("memory.signals.extract_observations_task") as task_mock:
            run_finished.send(sender=Run, run=run)
        task_mock.enqueue.assert_called_once_with(str(run.pk))


@pytest.mark.django_db
class TestNotificationsSkipChatRuns:
    """on_run_finished must ignore CHAT-triggered runs."""

    def test_notifications_skip_chat_runs(self, member_user):
        session = _session(origin=SessionOrigin.CHAT, thread_id="chat-notif", user=member_user)
        run = _run(session, trigger_type=SessionOrigin.CHAT, user=member_user)
        run_finished.send(sender=Run, run=run)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_notifications_process_api_job_runs(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        session = _session(user=member_user)
        run = _run(session, user=member_user)
        run_finished.send(sender=Run, run=run)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_notifications_process_schedule_runs(self, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="nightly",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ALWAYS,
        )
        session = _session(origin=SessionOrigin.SCHEDULE, thread_id="sched-thread", scheduled_job=schedule)
        run = _run(session, trigger_type=SessionOrigin.SCHEDULE, user=member_user)
        run_finished.send(sender=Run, run=run)
        assert Notification.objects.filter(recipient=member_user, event_type="schedule.finished").count() == 1
