"""Tests for notifications receivers wired to sessions.signals.run_finished."""

import logging
import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import ChannelType, NotifyOn
from notifications.models import Notification, NotificationDelivery
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin
from sessions.signals import run_classified, run_finished

from accounts.models import User


@pytest.fixture(autouse=True)
def _isolate_memory_extraction():
    """``run_finished`` also drives memory extraction. Stub the task so these
    notification tests stay hermetic and don't contend on the SQLite write lock via
    the synchronous task backend. Tests that assert on the memory task patch it
    themselves (the inner patch wins for their duration)."""
    with patch("memory.signals.extract_observations_task"):
        yield


def _session(*, origin=SessionOrigin.API_JOB, thread_id="thread-run-1", repo_id="x/y", **kwargs):
    return Session.objects.create(thread_id=thread_id, origin=origin, repo_id=repo_id, **kwargs)


def _run(session, *, trigger_type=SessionOrigin.API_JOB, status=RunStatus.SUCCESSFUL, repo_id="x/y", **kwargs):
    return Run.objects.create(session=session, trigger_type=trigger_type, status=status, repo_id=repo_id, **kwargs)


def _classified_run(
    session,
    *,
    status=EnvelopeStatus.FOUND_ISSUES,
    count=1,
    summary="s",
    trigger_type=SessionOrigin.API_JOB,
    muted=None,
    user=None,
    finished_at=None,
    **kwargs,
):
    run = _run(
        session,
        trigger_type=trigger_type,
        status=RunStatus.SUCCESSFUL,
        user=user,
        muted=muted,
        finished_at=finished_at or timezone.now(),
        **kwargs,
    )
    actionable = (
        [{"id": "0", "kind": "bug", "label": "x", "ref": "y", "schema_version": 1}]
        if status == EnvelopeStatus.FOUND_ISSUES and count
        else []
    )
    envelope = RunEnvelope.objects.create(run=run, status=status, summary=summary, actionable=actionable)
    return run, envelope


def _make_run_batch(user, *, statuses, repos=None, batch_id=None, notify_on=None, scheduled_job=None):
    """Create one Run per status, each in its own Session, all sharing one ``batch_id``.

    Mirrors the old activity-batch helper. Siblings live in separate sessions so the
    one-active-per-session constraint never fires (batch grouping is by ``batch_id``,
    which spans sessions).
    """
    bid = batch_id or uuid.uuid4()
    repos = repos or [f"acme/repo{i}" for i in range(len(statuses))]
    trigger = SessionOrigin.SCHEDULE if scheduled_job else SessionOrigin.API_JOB
    runs = []
    for i, status in enumerate(statuses):
        session = Session.objects.create(
            thread_id=str(uuid.uuid4()), origin=trigger, repo_id=repos[i], user=user, scheduled_job=scheduled_job
        )
        runs.append(
            Run.objects.create(
                session=session,
                trigger_type=trigger,
                repo_id=repos[i],
                status=status,
                user=user,
                batch_id=bid,
                notify_on=notify_on,
            )
        )
    return runs


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
class TestRunBatchRollup:
    """Ported from the old activity-batch suite; the batch path (``_handle_batch_completion_run``)
    is live receiver code that the sessions refactor left uncovered."""

    def test_intermediate_sibling_does_not_emit(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        a, _b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.RUNNING])
        run_finished.send(sender=Run, run=a)

        assert Notification.objects.count() == 0

    def test_two_job_batch_emits_one_rollup_after_both_finish(self, member_user, email_binding):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        run_finished.send(sender=Run, run=a)
        run_finished.send(sender=Run, run=b)

        rollups = Notification.objects.filter(recipient=member_user, event_type="job_batch.finished")
        assert rollups.count() == 1
        rollup = rollups.get()
        assert rollup.source_type == "sessions.Batch"
        assert rollup.source_id == str(a.batch_id)
        assert rollup.link_url.endswith(f"?batch={a.batch_id}")
        assert "2" in rollup.subject and "succeed" in rollup.subject.lower()
        assert NotificationDelivery.objects.filter(notification=rollup, channel_type=ChannelType.EMAIL).count() == 1

    def test_single_job_batch_falls_back_to_per_run_notification(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        (a,) = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL])
        run_finished.send(sender=Run, run=a)

        assert Notification.objects.filter(event_type="job.finished").count() == 1
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0

    def test_mixed_outcomes_uses_failed_status_for_gating(self, member_user, email_binding):
        """notify_on=ON_FAILURE + a batch with at least one failure → email is delivered."""
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        a, b, c = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL, RunStatus.FAILED])
        for run in (a, b, c):
            run_finished.send(sender=Run, run=run)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert "1 failed" in rollup.body or "1 failed" in rollup.subject
        assert rollup.context["failed_count"] == 1
        assert rollup.context["successful_count"] == 2
        assert rollup.context["is_successful"] is False
        assert NotificationDelivery.objects.filter(notification=rollup, channel_type=ChannelType.EMAIL).count() == 1

    def test_all_succeed_with_notify_on_on_failure_writes_bell_only(self, member_user, email_binding):
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        run_finished.send(sender=Run, run=a)
        run_finished.send(sender=Run, run=b)

        assert Notification.objects.filter(event_type="job_batch.finished").count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_race_two_simultaneous_terminal_emissions_create_one_notification(self, member_user, caplog):
        """Both siblings observe terminal_count == total before either inserts; the second
        emission must hit the IntegrityError-recovery path (guards the partial unique constraint)."""
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = _make_run_batch(member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL])
        run_finished.send(sender=Run, run=a)
        with caplog.at_level(logging.DEBUG, logger="daiv.notifications"):
            run_finished.send(sender=Run, run=b)

        assert Notification.objects.filter(event_type="job_batch.finished").count() == 1
        assert any("already exists" in rec.message for rec in caplog.records)

    def test_schedule_batch_uses_schedule_name_and_fans_out_to_subscribers(self, member_user, run_schedule):
        sub = User.objects.create_user(username="batch_sub", email="batch_sub@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub)

        a, b = _make_run_batch(
            member_user, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL], scheduled_job=run_schedule
        )
        run_finished.send(sender=Run, run=a)
        run_finished.send(sender=Run, run=b)

        owner_rollup = Notification.objects.get(recipient=member_user, event_type="job_batch.finished")
        sub_rollup = Notification.objects.get(recipient=sub, event_type="job_batch.finished")
        assert run_schedule.name in owner_rollup.subject
        assert run_schedule.name in sub_rollup.subject
        assert str(member_user) in sub_rollup.subject
        assert owner_rollup.context["trigger_name"] == run_schedule.name
        assert sub_rollup.context["trigger_owner"] == str(member_user)

    def test_user_none_does_not_emit_rollup(self):
        a, b = _make_run_batch(None, statuses=[RunStatus.SUCCESSFUL, RunStatus.SUCCESSFUL], notify_on=NotifyOn.ALWAYS)
        run_finished.send(sender=Run, run=a)
        run_finished.send(sender=Run, run=b)

        assert Notification.objects.count() == 0

    def test_webhook_trigger_skipped_before_batch_branch(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        bid = uuid.uuid4()
        session = _session(origin=SessionOrigin.ISSUE_WEBHOOK, thread_id=str(uuid.uuid4()), user=member_user)
        a = _run(session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, batch_id=bid)
        b = _run(session, trigger_type=SessionOrigin.ISSUE_WEBHOOK, batch_id=bid)
        run_finished.send(sender=Run, run=a)
        run_finished.send(sender=Run, run=b)

        assert Notification.objects.count() == 0


@pytest.mark.django_db
class TestNotificationPolicy:
    def test_all_clear_writes_nothing(self, member_user):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.ALL_CLEAR, count=0, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    @pytest.mark.parametrize(
        "status", [EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION, EnvelopeStatus.FAILED]
    )
    def test_notify_worthy_writes_bell_and_channels(self, member_user, email_binding, status):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=status, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1
        assert NotificationDelivery.objects.filter(channel_type=ChannelType.EMAIL).count() == 1

    def test_muted_run_is_fully_silent(self, member_user, email_binding):
        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.FOUND_ISSUES, muted=True, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_muted_schedule_suppressed_and_run_override_unmutes(self, member_user, run_schedule):
        run_schedule.muted = True
        run_schedule.save(update_fields=["muted"])
        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FAILED, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

        session2 = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run2, envelope2 = _classified_run(
            session2, status=EnvelopeStatus.FAILED, trigger_type=SessionOrigin.SCHEDULE, user=member_user, muted=False
        )
        run_classified.send(sender=Run, run=run2, envelope=envelope2)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_stale_run_outside_window_suppressed(self, member_user, email_binding):
        from datetime import timedelta

        from sessions.tasks import RECLASSIFY_MAX_AGE

        session = _session(user=member_user)
        run, envelope = _classified_run(
            session,
            status=EnvelopeStatus.FAILED,
            user=member_user,
            finished_at=timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=1),
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 0

    def test_webhook_run_notifies_run_user_on_notify_worthy(self, member_user, email_binding):
        session = _session(origin=SessionOrigin.ISSUE_WEBHOOK, thread_id=str(uuid.uuid4()), user=member_user)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.ISSUE_WEBHOOK, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_idempotent_second_emit_is_deduped(self, member_user, caplog):
        import logging

        session = _session(user=member_user)
        run, envelope = _classified_run(session, status=EnvelopeStatus.FAILED, user=member_user)
        run_classified.send(sender=Run, run=run, envelope=envelope)
        with caplog.at_level(logging.DEBUG, logger="daiv.notifications"):
            run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1


@pytest.mark.django_db
class TestRunFanoutToSubscribers:
    """Single-run schedule notifications fan out to owner + subscribers."""

    def test_owner_plus_two_subscribers_each_get_one_notification(self, member_user, run_schedule):
        sub1 = User.objects.create_user(username="sub1", email="sub1@test.com", password="x")  # noqa: S106
        sub2 = User.objects.create_user(username="sub2", email="sub2@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub1, sub2)

        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 1
        assert Notification.objects.filter(recipient=sub2).count() == 1

    def test_owner_accidentally_in_subscribers_still_one_notification(self, member_user, run_schedule):
        run_schedule.subscribers.add(member_user)
        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_one_recipient_failure_does_not_block_others(self, member_user, run_schedule, mocker):
        from notifications.services import notify as real_notify

        sub1 = User.objects.create_user(username="fsub1", email="fsub1@test.com", password="x")  # noqa: S106
        sub2 = User.objects.create_user(username="fsub2", email="fsub2@test.com", password="x")  # noqa: S106
        run_schedule.subscribers.add(sub1, sub2)

        def flaky_notify(*, recipient, **kwargs):
            if recipient.pk == sub1.pk:
                raise RuntimeError("boom")
            return real_notify(recipient=recipient, **kwargs)

        mocker.patch("notifications.signals.notify", side_effect=flaky_notify)

        session = _session(origin=SessionOrigin.SCHEDULE, thread_id=str(uuid.uuid4()), scheduled_job=run_schedule)
        run, envelope = _classified_run(
            session, status=EnvelopeStatus.FOUND_ISSUES, trigger_type=SessionOrigin.SCHEDULE, user=member_user
        )
        run_classified.send(sender=Run, run=run, envelope=envelope)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 0
        assert Notification.objects.filter(recipient=sub2).count() == 1
