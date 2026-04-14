from unittest.mock import patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished
from notifications.choices import ChannelType, NotifyOn
from notifications.models import Notification, UserChannelBinding

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def schedule(member_user, email_binding):
    return ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repo_id="x/y",
        frequency=Frequency.DAILY,
        time="12:00",
        notify_on=NotifyOn.ALWAYS,
    )


@pytest.mark.django_db
class TestOnActivityFinished:
    def test_notifies_on_success_when_always(self, member_user, schedule):
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert Notification.objects.filter(recipient=member_user, event_type="schedule.finished").count() == 1

    def test_skips_when_no_schedule(self, member_user):
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, user=member_user, repo_id="x/y", status=ActivityStatus.SUCCESSFUL
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_skips_when_notify_on_never(self, member_user, schedule):
        schedule.notify_on = NotifyOn.NEVER
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_on_failure_skips_success(self, member_user, schedule):
        schedule.notify_on = NotifyOn.ON_FAILURE
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_on_failure_sends_on_failure(self, member_user, schedule):
        schedule.notify_on = NotifyOn.ON_FAILURE
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.FAILED,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 1

    def test_on_success_sends_on_success(self, member_user, schedule):
        schedule.notify_on = NotifyOn.ON_SUCCESS
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 1

    def test_on_success_skips_failure(self, member_user, schedule):
        schedule.notify_on = NotifyOn.ON_SUCCESS
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.FAILED,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_exception_in_notify_is_swallowed(self, member_user, schedule):
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        with patch("notifications.signals.notify", side_effect=RuntimeError("boom")):
            activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0


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
