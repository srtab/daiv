from unittest.mock import patch

import pytest
from notifications.choices import DeliveryStatus
from notifications.exceptions import UnrecoverableDeliveryError
from notifications.models import Notification, NotificationDelivery, UserChannelBinding
from notifications.tasks import _deliver_notification


@pytest.fixture
def notification_and_delivery(member_user):
    # Use get_or_create so this stays compatible once Task 14 (auto-seeder) is in place.
    UserChannelBinding.objects.get_or_create(
        user=member_user, channel_type="email", defaults={"address": member_user.email, "is_verified": True}
    )
    n = Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
    )
    d = NotificationDelivery.objects.create(notification=n, channel_type="email", address=member_user.email)
    return n, d


@pytest.mark.django_db
class TestDeliverNotification:
    def test_success_marks_sent(self, notification_and_delivery):
        n, d = notification_and_delivery
        with patch("notifications.channels.email.EmailChannel.send") as mock_send:
            mock_send.return_value = None
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.SENT
        assert d.attempts == 1
        assert d.delivered_at is not None
        assert d.last_attempted_at is not None
        mock_send.assert_called_once()

    def test_unrecoverable_error_marks_failed_no_retry(self, notification_and_delivery):
        n, d = notification_and_delivery
        with patch(
            "notifications.channels.email.EmailChannel.send", side_effect=UnrecoverableDeliveryError("bad address")
        ):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 1
        assert "bad address" in d.error_message

    def test_transient_error_within_max_attempts_stays_pending(self, notification_and_delivery):
        n, d = notification_and_delivery
        with patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 1
        assert "timeout" in d.error_message

    def test_transient_error_at_max_attempts_marks_failed(self, notification_and_delivery):
        n, d = notification_and_delivery
        d.attempts = 2
        d.save()
        with patch("notifications.channels.email.EmailChannel.send", side_effect=ConnectionError("timeout")):
            _deliver_notification(d.id)

        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.attempts == 3

    def test_skipped_delivery_is_not_processed(self, notification_and_delivery):
        n, d = notification_and_delivery
        d.status = DeliveryStatus.SKIPPED
        d.save()
        with patch("notifications.channels.email.EmailChannel.send") as mock_send:
            _deliver_notification(d.id)
        mock_send.assert_not_called()
