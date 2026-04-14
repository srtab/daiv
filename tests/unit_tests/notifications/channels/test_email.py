from django.core import mail

import pytest
from notifications.channels.email import EmailChannel
from notifications.models import Notification, NotificationDelivery, UserChannelBinding


@pytest.mark.django_db
class TestEmailChannelResolveAddress:
    def test_returns_binding_address(self, member_user):
        UserChannelBinding.objects.create(
            user=member_user, channel_type="email", address="alt@test.com", is_verified=True
        )
        assert EmailChannel().resolve_address(member_user) == "alt@test.com"

    def test_returns_none_when_no_binding(self, member_user):
        UserChannelBinding.objects.filter(user=member_user, channel_type="email").delete()
        assert EmailChannel().resolve_address(member_user) is None

    def test_skips_unverified_bindings(self, member_user):
        UserChannelBinding.objects.filter(user=member_user, channel_type="email").delete()
        UserChannelBinding.objects.create(
            user=member_user, channel_type="email", address="unverified@test.com", is_verified=False
        )
        assert EmailChannel().resolve_address(member_user) is None

    def test_prefers_most_recently_updated_verified_binding(self, member_user):
        UserChannelBinding.objects.filter(user=member_user, channel_type="email").delete()
        UserChannelBinding.objects.create(
            user=member_user, channel_type="email", address="old@test.com", is_verified=True
        )
        newer = UserChannelBinding.objects.create(
            user=member_user, channel_type="email", address="new@test.com", is_verified=True
        )
        newer.save()  # bump modified
        assert EmailChannel().resolve_address(member_user) == "new@test.com"


@pytest.mark.django_db
class TestEmailChannelSend:
    def test_sends_email_via_django_outbox(self, member_user):
        n = Notification.objects.create(
            recipient=member_user,
            event_type="schedule.finished",
            subject="Job finished",
            body="Your job has finished.",
            link_url="http://test/dashboard/activity/abc/",
        )
        d = NotificationDelivery.objects.create(notification=n, channel_type="email", address="a@test.com")
        EmailChannel().send(n, d)

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.subject == "Job finished"
        assert message.to == ["a@test.com"]
        assert "Your job has finished." in message.body
