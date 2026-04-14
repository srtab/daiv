from django.db import IntegrityError

import pytest
from notifications.choices import DeliveryStatus
from notifications.models import Notification, NotificationDelivery, UserChannelBinding


@pytest.mark.django_db
class TestNotification:
    def test_create_minimal(self, member_user):
        n = Notification.objects.create(
            recipient=member_user,
            event_type="schedule.finished",
            subject="Hello",
            body="Body",
            link_url="/dashboard/activity/123/",
        )
        assert n.id is not None
        assert n.read_at is None
        assert n.created is not None
        assert n.context == {}


@pytest.mark.django_db
class TestNotificationDelivery:
    def test_defaults(self, member_user):
        n = Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
        )
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 0
        assert d.delivered_at is None

    def test_unique_per_channel_per_notification(self, member_user):
        n = Notification.objects.create(
            recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
        )
        NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        with pytest.raises(IntegrityError):
            NotificationDelivery.objects.create(notification=n, channel_type="email", address="other@b.com")


@pytest.mark.django_db
class TestUserChannelBinding:
    def test_unique_user_channel_address(self, member_user):
        # Use a non-email channel to avoid collision with Task 14's auto-seeded email binding.
        UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")
        with pytest.raises(IntegrityError):
            UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")
