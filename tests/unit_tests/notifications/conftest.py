from django.utils import timezone

import pytest
from notifications.choices import ChannelType
from notifications.models import Notification, NotificationDelivery, UserChannelBinding


@pytest.fixture
def email_binding(member_user):
    """Ensure the member_user has a verified email channel binding."""
    binding, _ = UserChannelBinding.objects.get_or_create(
        user=member_user,
        channel_type=ChannelType.EMAIL,
        defaults={"address": member_user.email, "is_verified": True, "verified_at": timezone.now()},
    )
    return binding


@pytest.fixture
def notification(member_user):
    return Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="Hi", body="Body", link_url="/x/"
    )


@pytest.fixture
def notification_with_delivery(member_user, email_binding):
    """Create a notification with a PENDING email delivery."""
    n = Notification.objects.create(
        recipient=member_user, event_type="schedule.finished", subject="s", body="b", link_url="/"
    )
    d = NotificationDelivery.objects.create(notification=n, channel_type="email", address=member_user.email)
    return n, d
