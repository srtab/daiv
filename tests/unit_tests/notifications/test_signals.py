from unittest.mock import patch

import pytest
from notifications.choices import ChannelType, EventType
from notifications.models import Notification, UserChannelBinding
from notifications.policy import SOURCE_SESSION
from sessions.models import Session, SessionOrigin
from sessions.signals import watch_exhausted

from accounts.models import User


@pytest.mark.django_db
class TestUserBindingSeeder:
    def test_creates_email_binding_on_user_create(self):
        user = User.objects.create_user(
            username="new",
            email="new@test.com",
            password="x",  # noqa: S106
        )
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 1

        binding = UserChannelBinding.objects.get(user=user, channel_type=ChannelType.EMAIL)
        assert binding.address == "new@test.com"
        assert binding.is_verified is True
        assert binding.verified_at is not None

    def test_updates_binding_on_email_change(self):
        user = User.objects.create_user(
            username="u",
            email="a@test.com",
            password="x",  # noqa: S106
        )
        user.email = "b@test.com"
        user.save()

        binding = UserChannelBinding.objects.get(user=user, channel_type=ChannelType.EMAIL)
        assert binding.address == "b@test.com"

    def test_idempotent_on_repeated_save_same_email(self):
        user = User.objects.create_user(
            username="u",
            email="a@test.com",
            password="x",  # noqa: S106
        )
        user.name = "New Name"
        user.save()
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 1

    def test_skips_user_without_email(self):
        user = User.objects.create_user(
            username="noemail",
            email="",
            password="x",  # noqa: S106
        )
        assert UserChannelBinding.objects.filter(user=user, channel_type=ChannelType.EMAIL).count() == 0


@pytest.mark.django_db
class TestOnWatchExhausted:
    @pytest.fixture
    def session(self, member_user):
        return Session.objects.create(
            thread_id="t3",
            origin=SessionOrigin.MR_WEBHOOK,
            repo_id="group/repo",
            merge_request_iid=9,
            watch_attempts=2,
            user=member_user,
        )

    def _send(self, session):
        watch_exhausted.send(
            sender=Session,
            session=session,
            failing_jobs=["tests"],
            pipeline_url="https://example.com/p/100",
            pipeline_id=100,
        )

    def test_the_notification_lands_under_the_shared_session_key(self, session):
        self._send(session)

        notification = Notification.objects.get()
        assert notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED
        assert notification.source_type == SOURCE_SESSION
        assert notification.source_id == "t3:100"
        assert notification.context["pipeline_url"] == "https://example.com/p/100"
        assert notification.context["failing_jobs"] == ["tests"]

    def test_a_failing_emitter_is_logged_not_raised(self, session, caplog):
        with patch("notifications.signals.emit_watch_exhausted", side_effect=RuntimeError("channel down")):
            self._send(session)

        assert "on_watch_exhausted: unexpected error for thread_id=t3" in caplog.text
