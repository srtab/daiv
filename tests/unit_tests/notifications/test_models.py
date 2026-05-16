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

    def test_mark_as_read(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        assert n.read_at is None
        n.mark_as_read()
        n.refresh_from_db()
        assert n.read_at is not None

    def test_mark_as_read_is_idempotent(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        n.mark_as_read()
        original = n.read_at
        n.mark_as_read()
        n.refresh_from_db()
        assert n.read_at == original

    def test_mark_all_read_for_returns_count_and_preserves_already_read(self, member_user):
        unread = [
            Notification.objects.create(recipient=member_user, event_type="e", subject=f"u{i}", body="b", link_url="/")
            for i in range(3)
        ]
        already_read = Notification.objects.create(
            recipient=member_user, event_type="e", subject="r", body="b", link_url="/"
        )
        already_read.mark_as_read()
        original_read_at = Notification.objects.get(pk=already_read.pk).read_at

        updated = Notification.mark_all_read_for(member_user)

        assert updated == len(unread)
        assert Notification.objects.filter(recipient=member_user, read_at__isnull=True).count() == 0
        # Already-read rows must keep their original read_at — the helper must scope to unread only.
        assert Notification.objects.get(pk=already_read.pk).read_at == original_read_at

    def test_mark_all_read_for_bumps_modified(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        original_modified = n.modified
        Notification.mark_all_read_for(member_user)
        n.refresh_from_db()
        assert n.modified > original_modified


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

    def test_mark_sent(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_sent()
        d.refresh_from_db()
        assert d.status == DeliveryStatus.SENT
        assert d.delivered_at is not None
        assert d.error_message == ""

    def test_mark_failed(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_failed("connection refused")
        d.refresh_from_db()
        assert d.status == DeliveryStatus.FAILED
        assert d.error_message == "connection refused"

    def test_mark_skipped(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        d.mark_skipped("no binding")
        d.refresh_from_db()
        assert d.status == DeliveryStatus.SKIPPED
        assert d.error_message == "no binding"

    def test_sent_without_delivered_at_violates_constraint(self, member_user):
        n = Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@b.com")
        with pytest.raises(IntegrityError):
            NotificationDelivery.objects.filter(pk=d.pk).update(status=DeliveryStatus.SENT, delivered_at=None)


@pytest.mark.django_db
class TestUserChannelBinding:
    def test_unique_user_channel_address(self, member_user):
        UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")
        with pytest.raises(IntegrityError):
            UserChannelBinding.objects.create(user=member_user, channel_type="slack", address="u-1")

    def test_verified_without_timestamp_violates_constraint(self, member_user):
        with pytest.raises(IntegrityError):
            UserChannelBinding.objects.create(
                user=member_user, channel_type="slack", address="u-1", is_verified=True, verified_at=None
            )
