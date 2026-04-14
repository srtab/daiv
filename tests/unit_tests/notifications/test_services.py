from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import pytest
from notifications.choices import ChannelType, DeliveryStatus
from notifications.models import Notification, UserChannelBinding
from notifications.services import create_notification, dispatch_notification, notify


@pytest.mark.django_db
class TestCreateNotification:
    def test_creates_notification_and_deliveries(self, member_user, email_binding):
        notification = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="Subject",
            body="Body",
            link_url="/x/",
            channels=[ChannelType.EMAIL],
        )
        assert isinstance(notification, Notification)
        assert notification.deliveries.count() == 1

        d = notification.deliveries.get()
        assert d.channel_type == ChannelType.EMAIL
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 0

    def test_address_is_resolved_from_channel(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.EMAIL,
            address="resolved@test.com",
            is_verified=True,
            verified_at=timezone.now(),
        )
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=[ChannelType.EMAIL],
        )
        assert n.deliveries.get().address == "resolved@test.com"

    def test_unresolved_address_is_skipped(self, member_user):
        UserChannelBinding.objects.filter(user=member_user, channel_type=ChannelType.EMAIL).delete()
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=[ChannelType.EMAIL],
        )
        d = n.deliveries.get()
        assert d.status == DeliveryStatus.SKIPPED
        assert d.error_message == "no binding"

    def test_unknown_channel_is_skipped(self, member_user):
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=["nonexistent"],
        )
        d = n.deliveries.get()
        assert d.status == DeliveryStatus.SKIPPED
        assert "unknown channel" in d.error_message.lower()


@pytest.mark.django_db
class TestDispatchNotification:
    def test_enqueue_failure_marks_delivery_failed(self, member_user, email_binding):
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=[ChannelType.EMAIL],
        )
        with patch("notifications.tasks.deliver_notification_task") as mock_task:
            mock_task.enqueue.side_effect = RuntimeError("broker down")
            dispatch_notification(n)

        d = n.deliveries.get()
        assert d.status == DeliveryStatus.FAILED
        assert "Failed to enqueue" in d.error_message


@pytest.mark.django_db
class TestNotify:
    def test_creates_notification_and_enqueues_on_commit(self, member_user, email_binding):
        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            notification = notify(
                recipient=member_user,
                event_type="schedule.finished",
                source_type="activity.Activity",
                source_id="abc",
                subject="Subject",
                body="Body",
                link_url="/x/",
                channels=[ChannelType.EMAIL],
            )

        assert notification.deliveries.filter(status=DeliveryStatus.PENDING).count() == 1
        assert len(callbacks) == 1
