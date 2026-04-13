from django.test import TestCase

import pytest
from notifications.choices import DeliveryStatus
from notifications.models import Notification, UserChannelBinding
from notifications.services import create_notification, notify


@pytest.mark.django_db
class TestCreateNotification:
    def test_creates_notification_and_deliveries(self, member_user):
        UserChannelBinding.objects.get_or_create(
            user=member_user, channel_type="email", address="member@test.com", defaults={"is_verified": True}
        )
        notification = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="Subject",
            body="Body",
            link_url="/x/",
            channels=["email"],
        )
        assert isinstance(notification, Notification)
        assert notification.deliveries.count() == 1

        d = notification.deliveries.get()
        assert d.channel_type == "email"
        assert d.status == DeliveryStatus.PENDING
        assert d.attempts == 0

    def test_address_is_resolved_from_channel(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type="email", address="resolved@test.com", is_verified=True
        )
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=["email"],
        )
        assert n.deliveries.get().address == "resolved@test.com"

    def test_unresolved_address_is_skipped(self, member_user):
        # Clear any existing email binding so no address resolves.
        UserChannelBinding.objects.filter(user=member_user, channel_type="email").delete()
        n = create_notification(
            recipient=member_user,
            event_type="schedule.finished",
            source_type="",
            source_id="",
            subject="s",
            body="b",
            link_url="/",
            channels=["email"],
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
class TestNotify:
    def test_creates_notification_and_enqueues_on_commit(self, member_user):
        UserChannelBinding.objects.get_or_create(
            user=member_user, channel_type="email", defaults={"address": member_user.email, "is_verified": True}
        )
        with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
            notification = notify(
                recipient=member_user,
                event_type="schedule.finished",
                source_type="activity.Activity",
                source_id="abc",
                subject="Subject",
                body="Body",
                link_url="/x/",
                channels=["email"],
            )

        assert notification.deliveries.filter(status="pending").count() == 1
        # One on_commit callback = the dispatch
        assert len(callbacks) == 1
