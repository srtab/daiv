import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished
from notifications.choices import NotifyOn
from notifications.models import Notification, UserChannelBinding

from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def schedule(member_user):
    # Use get_or_create so this stays compatible once Task 14 (auto-seeder) is in place.
    UserChannelBinding.objects.get_or_create(
        user=member_user, channel_type="email", defaults={"address": member_user.email, "is_verified": True}
    )
    return ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repo_id="x/y",
        frequency=Frequency.DAILY,
        time="12:00",
        notify_on=NotifyOn.ALWAYS,
        notify_channels=["email"],
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
