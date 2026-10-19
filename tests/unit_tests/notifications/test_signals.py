from datetime import UTC, datetime, time
from unittest.mock import patch

import pytest
from notifications.choices import ChannelType, EventType
from notifications.models import Notification, UserChannelBinding
from sessions.models import Session
from sessions.signal_defs import watch_exhausted

from accounts.models import User
from schedules.models import DispatchError, Frequency, ScheduledJob
from schedules.signals import schedule_dispatch_failed


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
    def _send(self, session):
        watch_exhausted.send(
            sender=Session,
            session=session,
            failing_jobs=["tests"],
            pipeline_url="https://example.com/p/100",
            pipeline_id=100,
        )

    def test_the_owner_is_told_the_watch_gave_up(self, watch_session, member_user):
        self._send(watch_session)

        notification = Notification.objects.get()
        assert notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED
        assert notification.recipient == member_user

    def test_a_failing_emitter_is_logged_not_raised(self, watch_session, caplog):
        with patch("notifications.signals.emit_watch_exhausted", side_effect=RuntimeError("channel down")):
            self._send(watch_session)

        assert f"on_watch_exhausted: unexpected error for thread_id={watch_session.thread_id}" in caplog.text


@pytest.mark.django_db
class TestOnScheduleDispatchFailed:
    @pytest.fixture
    def failing_schedule(self, member_user):
        return ScheduledJob.objects.create(
            user=member_user,
            name="daily",
            prompt="p",
            repos=[{"repo_id": "acme/repo", "ref": ""}],
            frequency=Frequency.DAILY,
            time=time(9, 0),
            failing_since=datetime(2026, 10, 1, 8, 30, tzinfo=UTC),
            dispatch_error=DispatchError.REPO_ACCESS_DENIED,
            dispatch_error_repo_ids=["acme/repo"],
        )

    def test_the_owner_is_told_the_schedule_cannot_run(self, failing_schedule, member_user):
        schedule_dispatch_failed.send(sender=ScheduledJob, schedule_pk=failing_schedule.pk)

        notification = Notification.objects.get()
        assert notification.event_type == EventType.SCHEDULE_DISPATCH_FAILED
        assert notification.recipient == member_user

    def test_a_failing_emitter_is_logged_not_raised(self, failing_schedule, caplog):
        with patch("notifications.signals.emit_schedule_dispatch_failed", side_effect=RuntimeError("render failed")):
            schedule_dispatch_failed.send(sender=ScheduledJob, schedule_pk=failing_schedule.pk)

        assert f"on_schedule_dispatch_failed: unexpected error for schedule={failing_schedule.pk}" in caplog.text
