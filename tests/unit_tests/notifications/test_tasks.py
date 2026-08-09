import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from notifications.choices import DeliveryStatus
from notifications.exceptions import UnknownChannelError, UnrecoverableDeliveryError
from notifications.models import Notification
from notifications.tasks import _deliver_notification, redrive_missing_notifications_cron_task
from sessions.models import EnvelopeStatus, Run, RunEnvelope, RunStatus, Session, SessionOrigin


@pytest.mark.django_db
class TestDeliverNotification:
    def test_success_marks_sent(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch("notifications.channels.email.EmailChannel.send"):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.SENT
        assert d.attempts == 1
        assert d.delivered_at is not None
        assert d.last_attempted_at is not None

    def test_unrecoverable_error_marks_failed_no_retry(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch(
            "notifications.channels.email.EmailChannel.send", side_effect=UnrecoverableDeliveryError("bad address")
        ):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 1
        assert "bad address" in d.error_message

    def test_transient_error_within_max_attempts_stays_pending(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with (
            patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")),
            patch("notifications.tasks.deliver_notification_task") as mock_task,
        ):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 1
        assert "timeout" in d.error_message
        mock_task.using.return_value.enqueue.assert_called_once()

    def test_transient_error_at_max_attempts_marks_failed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        d.attempts = 2
        d.save()
        with patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 3

    def test_skipped_delivery_is_not_processed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        d.status = DeliveryStatus.SKIPPED
        d.save()
        with patch("notifications.channels.email.EmailChannel.send") as mock_send:
            _deliver_notification(d.id)
        mock_send.assert_not_called()

    def test_nonexistent_delivery_does_not_raise(self):
        _deliver_notification(uuid.uuid4())

    def test_unknown_channel_at_delivery_time(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with patch("notifications.tasks.get_channel", side_effect=UnknownChannelError("sms")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.SKIPPED
        assert "sms" in d.error_message

    def test_reenqueue_failure_marks_failed(self, notification_with_delivery):
        _n, d = notification_with_delivery
        with (
            patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")),
            patch("notifications.tasks.deliver_notification_task") as mock_task,
        ):
            mock_task.using.return_value.enqueue.side_effect = RuntimeError("broker down")
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert "Re-enqueue failed" in d.error_message


def _classified_finished_run(user, *, status=EnvelopeStatus.FAILED):
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.API_JOB, repo_id="x/y", user=user
    )
    run = Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.API_JOB,
        repo_id="x/y",
        status=RunStatus.FAILED if status == EnvelopeStatus.FAILED else RunStatus.SUCCESSFUL,
        user=user,
        finished_at=timezone.now(),
    )
    RunEnvelope.objects.create(run=run, status=status, summary="s")
    return run


@pytest.mark.django_db
def test_redrive_delivers_missing_notification(member_user, email_binding):
    run = _classified_finished_run(member_user)  # envelope exists, but no Notification (crash-window sim)
    assert Notification.objects.filter(source_id=str(run.pk)).count() == 0
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk)).count() == 1


@pytest.mark.django_db
def test_redrive_second_pass_does_not_duplicate(member_user, email_binding):
    run = _classified_finished_run(member_user)
    redrive_missing_notifications_cron_task.func()
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.filter(source_type="sessions.Run", source_id=str(run.pk)).count() == 1


@pytest.mark.django_db
def test_redrive_skips_all_clear_and_out_of_window(member_user, email_binding):
    from datetime import timedelta

    from sessions.tasks import RECLASSIFY_MAX_AGE

    _classified_finished_run(member_user, status=EnvelopeStatus.ALL_CLEAR)  # not notify-worthy
    stale = _classified_finished_run(member_user)
    Run.objects.filter(pk=stale.pk).update(finished_at=timezone.now() - RECLASSIFY_MAX_AGE - timedelta(hours=2))
    redrive_missing_notifications_cron_task.func()
    assert Notification.objects.count() == 0
