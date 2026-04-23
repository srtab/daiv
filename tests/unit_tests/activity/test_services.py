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

    def test_schedule_run_defers_to_schedule_when_no_override(self, member_user):
        """Without an explicit override, activity.notify_on stays null and the effective
        value falls through to ScheduledJob.notify_on via Activity.effective_notify_on."""
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
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
        assert activity.notify_on is None
        assert activity.effective_notify_on == NotifyOn.ALWAYS

    def test_explicit_notify_on_beats_schedule_default(self, member_user):
        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
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
        assert activity.effective_notify_on == NotifyOn.NEVER


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


@pytest.mark.django_db
class TestEffectiveNotifyOn:
    def test_run_override_wins_over_user_default(self, member_user):
        from activity.models import Activity

        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y", notify_on=NotifyOn.ALWAYS
        )
        assert activity.effective_notify_on == NotifyOn.ALWAYS

    def test_falls_back_to_user_default_when_no_override(self, member_user):
        from activity.models import Activity

        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y")
        assert activity.effective_notify_on == NotifyOn.ON_FAILURE

    def test_schedule_default_used_when_no_override(self, member_user):
        from activity.models import Activity

        schedule = ScheduledJob.objects.create(
            user=member_user,
            name="s",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="12:00",
            notify_on=NotifyOn.ON_SUCCESS,
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE, user=member_user, repo_id="x/y", scheduled_job=schedule
        )
        assert activity.effective_notify_on == NotifyOn.ON_SUCCESS

    def test_returns_never_when_no_user_no_schedule_no_override(self):
        from activity.models import Activity

        activity = Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="x/y")
        assert activity.effective_notify_on == NotifyOn.NEVER
