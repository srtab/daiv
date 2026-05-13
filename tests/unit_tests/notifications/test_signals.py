import logging
import uuid
from unittest.mock import patch

from django.utils import timezone

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished
from notifications.choices import ChannelType, NotifyOn
from notifications.models import Notification, NotificationDelivery, UserChannelBinding

from accounts.models import User
from schedules.models import Frequency, ScheduledJob


@pytest.fixture
def schedule(member_user, email_binding):
    return ScheduledJob.objects.create(
        user=member_user,
        name="s",
        prompt="p",
        repos=[{"repo_id": "x/y", "ref": ""}],
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

    def test_api_job_successful_writes_bell_without_email_when_user_default_on_failure(self, member_user):
        """Bell row is always written for a terminal activity with a recipient;
        ``notify_on`` only gates external delivery channels."""
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB, user=member_user, repo_id="x/y", status=ActivityStatus.SUCCESSFUL
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_notify_on_never_writes_bell_without_email(self, member_user, schedule):
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
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_on_failure_writes_bell_on_success_without_email(self, member_user, schedule):
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
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

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
        assert NotificationDelivery.objects.filter(channel_type=ChannelType.EMAIL).count() == 1

    def test_on_success_writes_bell_on_failure_without_email(self, member_user, schedule):
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
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_body_excludes_result_summary_and_context_carries_metadata(self, member_user, schedule):
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="acme/app",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
            result_summary="# Heading\n\nSome **markdown** that must not leak into email body.",
        )
        activity_finished.send(sender=Activity, activity=activity)

        n = Notification.objects.get(recipient=member_user, event_type="schedule.finished")
        assert "markdown" not in n.body
        assert "Heading" not in n.body
        assert schedule.name in n.body
        assert n.context["status"] == ActivityStatus.SUCCESSFUL
        assert n.context["is_successful"] is True
        assert n.context["status_label"] == ActivityStatus.SUCCESSFUL.label
        assert n.context["trigger_name"] == schedule.name
        assert n.context["trigger_label"]
        assert n.context["repo_id"] == "acme/app"
        assert "duration_seconds" in n.context

    def test_body_excludes_error_message_on_failure(self, member_user, schedule):
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="acme/app",
            status=ActivityStatus.FAILED,
            scheduled_job=schedule,
            error_message="Traceback (most recent call last): secret stacktrace",
        )
        activity_finished.send(sender=Activity, activity=activity)

        n = Notification.objects.get(recipient=member_user, event_type="schedule.finished")
        assert "Traceback" not in n.body
        assert "stacktrace" not in n.body
        assert schedule.name in n.body
        assert n.context["is_successful"] is False
        assert n.context["status_label"] == ActivityStatus.FAILED.label

    def test_schedule_override_never_silences_email_but_keeps_bell(self, member_user, schedule):
        """Per-run notify_on=NEVER overrides schedule.notify_on=ALWAYS for email, but the
        bell row is still written so the activity remains visible in history."""
        assert schedule.notify_on == NotifyOn.ALWAYS  # fixture baseline
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
            notify_on=NotifyOn.NEVER,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_schedule_override_always_beats_schedule_never(self, member_user, schedule):
        """Per-run notify_on=ALWAYS must override schedule.notify_on=NEVER."""
        schedule.notify_on = NotifyOn.NEVER
        schedule.save()
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
            notify_on=NotifyOn.ALWAYS,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user, event_type="schedule.finished").count() == 1

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
class TestFanoutToSubscribers:
    def _make_user(self, username):
        return User.objects.create_user(
            username=username,
            email=f"{username}@test.com",
            password="x",  # noqa: S106
        )

    def test_owner_plus_two_subscribers_each_get_one_notification(self, member_user, schedule):
        sub1 = self._make_user("sub1")
        sub2 = self._make_user("sub2")
        schedule.subscribers.add(sub1, sub2)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 1
        assert Notification.objects.filter(recipient=sub2).count() == 1

    def test_owner_accidentally_in_subscribers_still_one_notification(self, member_user, schedule):
        schedule.subscribers.add(member_user)
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    def test_notify_on_never_skips_email_for_all_subscribers_but_writes_bell(self, member_user, schedule):
        schedule.notify_on = NotifyOn.NEVER
        schedule.save()
        sub = self._make_user("sub1")
        schedule.subscribers.add(sub)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_one_recipient_failure_does_not_block_others(self, member_user, schedule, mocker):
        from notifications.services import notify as real_notify

        sub1 = self._make_user("sub1")
        sub2 = self._make_user("sub2")
        schedule.subscribers.add(sub1, sub2)

        def flaky_notify(*, recipient, **kwargs):
            if recipient.pk == sub1.pk:
                raise RuntimeError("boom")
            return real_notify(recipient=recipient, **kwargs)

        mocker.patch("notifications.signals.notify", side_effect=flaky_notify)

        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert Notification.objects.filter(recipient=sub1).count() == 0
        assert Notification.objects.filter(recipient=sub2).count() == 1


@pytest.mark.django_db
class TestJobActivityNotifications:
    def test_ui_job_failed_notifies_when_user_default_on_failure(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y", status=ActivityStatus.FAILED
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user, event_type="job.finished").count() == 1

    def test_ui_job_successful_writes_bell_without_email_when_user_default_on_failure(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="x/y", status=ActivityStatus.SUCCESSFUL
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_per_run_override_never_writes_bell_without_email(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            notify_on=NotifyOn.NEVER,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_per_run_override_always_beats_user_default_never(self, member_user):
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            notify_on=NotifyOn.ALWAYS,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.filter(recipient=member_user).count() == 1

    @pytest.mark.parametrize("trigger", [TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK])
    def test_webhook_triggers_never_notify(self, member_user, trigger):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=trigger, user=member_user, repo_id="x/y", status=ActivityStatus.SUCCESSFUL
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_user_none_does_not_notify_or_crash(self):
        activity = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            user=None,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            notify_on=NotifyOn.ALWAYS,
        )
        activity_finished.send(sender=Activity, activity=activity)
        assert Notification.objects.count() == 0

    def test_job_rendered_subject_and_event_type(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        activity = Activity.objects.create(
            trigger_type=TriggerType.UI_JOB, user=member_user, repo_id="acme/app", status=ActivityStatus.FAILED
        )
        activity_finished.send(sender=Activity, activity=activity)

        n = Notification.objects.get(recipient=member_user, event_type="job.finished")
        assert "acme/app" in n.subject
        assert "failed" in n.subject.lower()
        assert n.context["trigger_label"]
        assert n.context["repo_id"] == "acme/app"


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
@pytest.mark.usefixtures("rocketchat_channel_enabled")
class TestRocketChatFanOut:
    """Verify RocketChatChannel is picked up by ``on_activity_finished`` when
    the user has a verified Rocket Chat binding."""

    def test_creates_rocketchat_delivery_when_binding_exists(self, member_user, schedule):
        UserChannelBinding.objects.create(
            user=member_user,
            channel_type=ChannelType.ROCKETCHAT,
            address="alice",
            is_verified=True,
            verified_at=timezone.now(),
        )
        activity = Activity.objects.create(
            trigger_type=TriggerType.SCHEDULE,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            scheduled_job=schedule,
        )
        activity_finished.send(sender=Activity, activity=activity)

        assert NotificationDelivery.objects.filter(channel_type=ChannelType.ROCKETCHAT).count() == 1
        delivery = NotificationDelivery.objects.get(channel_type=ChannelType.ROCKETCHAT)
        assert delivery.address == "alice"


@pytest.mark.django_db
class TestBatchRollup:
    def _make_batch(self, member_user, *, statuses, repos=None, batch_id=None, notify_on=None, scheduled_job=None):
        """Create one Activity per status; siblings share batch_id, user, notify_on."""
        bid = batch_id or uuid.uuid4()
        repos = repos or [f"acme/repo{i}" for i in range(len(statuses))]
        return [
            Activity.objects.create(
                trigger_type=TriggerType.SCHEDULE if scheduled_job else TriggerType.API_JOB,
                user=member_user,
                repo_id=repos[i],
                status=status,
                batch_id=bid,
                notify_on=notify_on,
                scheduled_job=scheduled_job,
            )
            for i, status in enumerate(statuses)
        ]

    def test_intermediate_sibling_does_not_emit(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.RUNNING])
        activity_finished.send(sender=Activity, activity=a)

        assert Notification.objects.count() == 0

    def test_two_job_batch_emits_one_rollup_after_both_finish(self, member_user, email_binding):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        rollups = Notification.objects.filter(recipient=member_user, event_type="job_batch.finished")
        assert rollups.count() == 1
        rollup = rollups.get()
        assert rollup.source_type == "activity.Batch"
        assert rollup.source_id == str(a.batch_id)
        assert rollup.link_url.endswith(f"?batch={a.batch_id}")
        assert "2" in rollup.subject and "succeed" in rollup.subject.lower()
        assert NotificationDelivery.objects.filter(notification=rollup, channel_type=ChannelType.EMAIL).count() == 1

    def test_single_job_batch_falls_back_to_per_activity_notification(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        (a,) = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL])
        activity_finished.send(sender=Activity, activity=a)

        assert Notification.objects.filter(event_type="job.finished").count() == 1
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0

    def test_mixed_outcomes_uses_failed_status_for_gating(self, member_user, email_binding):
        """notify_on=ON_FAILURE + batch with at least one failure → email is delivered."""
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        a, b, c = self._make_batch(
            member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED]
        )
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)
        activity_finished.send(sender=Activity, activity=c)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert "1 failed" in rollup.body or "1 failed" in rollup.subject
        assert rollup.context["failed_count"] == 1
        assert rollup.context["successful_count"] == 2
        assert rollup.context["is_successful"] is False
        assert NotificationDelivery.objects.filter(notification=rollup, channel_type=ChannelType.EMAIL).count() == 1

    def test_all_succeed_with_notify_on_on_failure_writes_bell_only(self, member_user, email_binding):
        member_user.notify_on_jobs = NotifyOn.ON_FAILURE
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        assert Notification.objects.filter(event_type="job_batch.finished").count() == 1
        assert NotificationDelivery.objects.count() == 0

    def test_race_two_simultaneous_terminal_emissions_create_one_notification(self, member_user, caplog):
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        # Both workers race and observe terminal_count == total before either inserts.
        activity_finished.send(sender=Activity, activity=a)
        with caplog.at_level(logging.DEBUG, logger="daiv.notifications"):
            activity_finished.send(sender=Activity, activity=b)

        assert Notification.objects.filter(event_type="job_batch.finished").count() == 1
        # Assert the second emission actually hit the IntegrityError-recovery path, so this
        # test would fail if the partial unique constraint were silently dropped.
        assert any("Batch rollup already exists" in rec.message for rec in caplog.records)

    def test_schedule_batch_uses_schedule_name_and_fans_out_to_subscribers(self, member_user, schedule):
        sub = User.objects.create_user(username="batch_sub", email="batch_sub@test.com", password="x")  # noqa: S106
        schedule.subscribers.add(sub)

        a, b = self._make_batch(
            member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL], scheduled_job=schedule
        )
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        owner_rollup = Notification.objects.get(recipient=member_user, event_type="job_batch.finished")
        sub_rollup = Notification.objects.get(recipient=sub, event_type="job_batch.finished")
        assert schedule.name in owner_rollup.subject
        assert schedule.name in sub_rollup.subject
        assert owner_rollup.context["trigger_name"] == schedule.name

    def test_user_none_does_not_emit_rollup(self):
        bid = uuid.uuid4()
        a = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            user=None,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
            notify_on=NotifyOn.ALWAYS,
        )
        b = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            user=None,
            repo_id="x/z",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
            notify_on=NotifyOn.ALWAYS,
        )
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        assert Notification.objects.count() == 0

    def test_webhook_trigger_skipped_before_batch_branch(self, member_user):
        member_user.notify_on_jobs = NotifyOn.ALWAYS
        member_user.save(update_fields=["notify_on_jobs"])

        bid = uuid.uuid4()
        a = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK,
            user=member_user,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
        )
        b = Activity.objects.create(
            trigger_type=TriggerType.ISSUE_WEBHOOK,
            user=member_user,
            repo_id="x/z",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
        )
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        assert Notification.objects.count() == 0

    def test_all_failed_subject_for_job_batch(self, member_user):
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.FAILED, ActivityStatus.FAILED])
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert "failed" in rollup.subject.lower()
        assert "2" in rollup.subject
        assert rollup.context["successful_count"] == 0
        assert rollup.context["failed_count"] == 2
        assert rollup.context["is_successful"] is False

    def test_all_failed_subject_for_schedule_batch(self, member_user, schedule):
        a, b = self._make_batch(
            member_user, statuses=[ActivityStatus.FAILED, ActivityStatus.FAILED], scheduled_job=schedule
        )
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        rollup = Notification.objects.get(recipient=member_user, event_type="job_batch.finished")
        assert schedule.name in rollup.subject
        assert "failed" in rollup.subject.lower()

    def test_summarize_repos_truncates_when_more_than_three(self, member_user):
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        statuses = [ActivityStatus.SUCCESSFUL] * 5
        repos = ["acme/a", "acme/b", "acme/c", "acme/d", "acme/e"]
        activities = self._make_batch(member_user, statuses=statuses, repos=repos)
        for activity in activities:
            activity_finished.send(sender=Activity, activity=activity)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert "and 2 more" in rollup.body
        assert rollup.context["repo_ids"] == sorted(repos)

    def test_batch_duration_computes_wall_clock_span(self, member_user):
        from datetime import timedelta

        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        base = timezone.now()
        # Earliest start 0s, latest finish 30s → wall-clock span = 30s.
        Activity.objects.filter(pk=a.pk).update(started_at=base, finished_at=base + timedelta(seconds=20))
        Activity.objects.filter(pk=b.pk).update(
            started_at=base + timedelta(seconds=10), finished_at=base + timedelta(seconds=30)
        )
        a.refresh_from_db()
        b.refresh_from_db()

        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert rollup.context["duration_seconds"] == pytest.approx(30.0)

    def test_batch_duration_is_none_when_timestamps_missing(self, member_user):
        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        activity_finished.send(sender=Activity, activity=a)
        activity_finished.send(sender=Activity, activity=b)

        rollup = Notification.objects.get(event_type="job_batch.finished")
        assert rollup.context["duration_seconds"] is None

    def test_unexpected_integrity_error_is_logged_not_swallowed(self, member_user, mocker, caplog):
        """A NOT NULL / FK / unrelated unique violation must not look like a race."""
        from django.db import IntegrityError

        member_user.notify_on_jobs = NotifyOn.NEVER
        member_user.save(update_fields=["notify_on_jobs"])

        a, b = self._make_batch(member_user, statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.SUCCESSFUL])
        mocker.patch("notifications.signals.notify", side_effect=IntegrityError("simulated FK violation"))

        with caplog.at_level(logging.ERROR, logger="daiv.notifications"):
            activity_finished.send(sender=Activity, activity=a)
            activity_finished.send(sender=Activity, activity=b)

        # No row was created (the simulated error means the insert never landed),
        # so the recovery probe sees no rollup and logs an exception instead of debug.
        assert Notification.objects.filter(event_type="job_batch.finished").count() == 0
        assert any("Unexpected IntegrityError" in rec.message for rec in caplog.records)

    def test_schedule_batch_mixed_outcomes_renders_count_in_subject(self, member_user, schedule):
        a, b, c = self._make_batch(
            member_user,
            statuses=[ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED, ActivityStatus.SUCCESSFUL],
            scheduled_job=schedule,
        )
        for activity in (a, b, c):
            activity_finished.send(sender=Activity, activity=activity)

        rollup = Notification.objects.get(recipient=member_user, event_type="job_batch.finished")
        assert schedule.name in rollup.subject
        assert "2/3" in rollup.subject

    def test_empty_recipients_on_multi_job_batch_logs_warning(self, caplog):
        bid = uuid.uuid4()
        a = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            user=None,
            repo_id="x/y",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
            notify_on=NotifyOn.ALWAYS,
        )
        b = Activity.objects.create(
            trigger_type=TriggerType.API_JOB,
            user=None,
            repo_id="x/z",
            status=ActivityStatus.SUCCESSFUL,
            batch_id=bid,
            notify_on=NotifyOn.ALWAYS,
        )
        with caplog.at_level(logging.WARNING, logger="daiv.notifications"):
            activity_finished.send(sender=Activity, activity=a)
            activity_finished.send(sender=Activity, activity=b)

        assert Notification.objects.count() == 0
        assert any("completed with no resolvable recipients" in rec.message for rec in caplog.records)
