import pytest
from activity.models import TriggerType
from activity.services import acreate_activity, create_activity
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
class TestCreateActivityNotifyOn:
    def test_explicit_notify_on_is_persisted(self, member_user):
        activity = create_activity(
            trigger_type=TriggerType.UI_JOB,
            task_result_id=None,
            repo_id="x/y",
            user=member_user,
            notify_on=NotifyOn.NEVER,
        )
        assert activity.notify_on == NotifyOn.NEVER

    def test_no_notify_on_leaves_null(self, member_user):
        activity = create_activity(
            trigger_type=TriggerType.UI_JOB, task_result_id=None, repo_id="x/y", user=member_user
        )
        assert activity.notify_on is None

    def test_schedule_run_copies_notify_on_when_not_provided(self, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repo_id="x/y",
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ALWAYS,
        )
        activity = create_activity(
            trigger_type=TriggerType.SCHEDULE,
            task_result_id=None,
            repo_id="x/y",
            scheduled_job=schedule,
            user=member_user,
        )
        assert activity.notify_on == NotifyOn.ALWAYS

    def test_explicit_notify_on_beats_schedule_default(self, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repo_id="x/y",
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ALWAYS,
        )
        activity = create_activity(
            trigger_type=TriggerType.SCHEDULE,
            task_result_id=None,
            repo_id="x/y",
            scheduled_job=schedule,
            user=member_user,
            notify_on=NotifyOn.NEVER,
        )
        assert activity.notify_on == NotifyOn.NEVER


@pytest.mark.django_db(transaction=True)
class TestAcreateActivityNotifyOn:
    async def test_async_variant_threads_notify_on(self, member_user):
        activity = await acreate_activity(
            trigger_type=TriggerType.API_JOB,
            task_result_id=None,
            repo_id="x/y",
            user=member_user,
            notify_on=NotifyOn.ON_FAILURE,
        )
        assert activity.notify_on == NotifyOn.ON_FAILURE
