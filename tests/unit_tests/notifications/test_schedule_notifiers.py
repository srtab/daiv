from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

import pytest
from notifications.channels.registry import enabled_channel_types
from notifications.choices import EventType
from notifications.models import Notification
from notifications.policy import SOURCE_SCHEDULE
from notifications.schedule_notifiers import emit_schedule_dispatch_failed

from schedules.models import DispatchError, Frequency, ScheduledJob

pytestmark = pytest.mark.django_db

FAILING_SINCE = datetime(2026, 10, 1, 8, 30, tzinfo=UTC)


@pytest.fixture
def failing_schedule(member_user):
    return ScheduledJob.objects.create(
        user=member_user,
        name="RT Daily Report",
        prompt="p",
        repos=[{"repo_id": "sfr/rt-daily-report", "ref": ""}],
        frequency=Frequency.WEEKDAYS,
        time=time(9, 0),
        failing_since=FAILING_SINCE,
        dispatch_error=DispatchError.REPO_ACCESS_DENIED,
        dispatch_error_repo_ids=["sfr/rt-daily-report"],
        last_run_at=FAILING_SINCE - timedelta(days=78),
    )


class TestSourceKey:
    def test_it_keys_on_the_schedule_and_its_failing_streak(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)

        notification = Notification.objects.get()
        assert notification.source_type == SOURCE_SCHEDULE
        assert notification.source_id == f"{failing_schedule.pk}:{FAILING_SINCE.isoformat()}"
        assert notification.event_type == EventType.SCHEDULE_DISPATCH_FAILED

    def test_a_repeat_emit_for_the_same_streak_delivers_once(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)
        with patch("notifications.schedule_notifiers.deliver_to_recipients") as deliver:
            emit_schedule_dispatch_failed(failing_schedule.pk)

        deliver.assert_not_called()
        assert Notification.objects.count() == 1

    def test_a_new_streak_notifies_again(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)
        failing_schedule.failing_since = FAILING_SINCE + timedelta(days=7)
        failing_schedule.save(update_fields=["failing_since"])
        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert Notification.objects.count() == 2

    def test_a_schedule_that_recovered_before_the_emit_is_not_notified(self, failing_schedule):
        failing_schedule.clear_dispatch_failure()
        failing_schedule.save()

        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert not Notification.objects.exists()

    def test_a_deleted_schedule_is_not_notified(self, failing_schedule):
        pk = failing_schedule.pk
        failing_schedule.delete()

        emit_schedule_dispatch_failed(pk)

        assert not Notification.objects.exists()


class TestRecipients:
    def test_only_the_owner_is_notified(self, failing_schedule, member_user, admin_user):
        failing_schedule.subscribers.add(admin_user)

        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert list(Notification.objects.values_list("recipient", flat=True)) == [member_user.pk]

    def test_a_muted_schedule_still_notifies(self, failing_schedule):
        # Mute silences run results; a schedule that can't run at all is a different, actionable problem.
        failing_schedule.muted = True
        failing_schedule.save(update_fields=["muted"])

        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert Notification.objects.count() == 1

    def test_it_delivers_on_every_enabled_channel(self, failing_schedule, email_binding):
        emit_schedule_dispatch_failed(failing_schedule.pk)

        notification = Notification.objects.get()
        assert sorted(notification.deliveries.values_list("channel_type", flat=True)) == sorted(enabled_channel_types())


class TestPayload:
    def test_it_links_to_the_schedule_edit_page(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert Notification.objects.get().link_url == reverse("schedule_update", args=[failing_schedule.pk])

    def test_access_denial_names_the_repo_and_says_it_tries_again(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)

        notification = Notification.objects.get()
        assert notification.subject == 'Schedule "RT Daily Report" can\'t run'
        assert "couldn't confirm that you have write access to sfr/rt-daily-report" in notification.body
        assert "check that your account is linked" in notification.body
        assert "DAIV tries again at the next scheduled time." in notification.body
        assert "You won't get this message again" in notification.body

    def test_unexpected_error_does_not_blame_access(self, failing_schedule):
        failing_schedule.dispatch_error = DispatchError.UNEXPECTED
        failing_schedule.dispatch_error_repo_ids = []
        failing_schedule.save()

        emit_schedule_dispatch_failed(failing_schedule.pk)

        body = Notification.objects.get().body
        assert "unexpected error" in body
        assert "write access" not in body

    def test_a_retired_one_off_says_how_to_retry(self, failing_schedule):
        failing_schedule.frequency = Frequency.ONCE
        failing_schedule.is_enabled = False
        failing_schedule.save()

        emit_schedule_dispatch_failed(failing_schedule.pk)

        body = Notification.objects.get().body
        assert "won't try again on its own" in body
        assert "give it a new date and time" in body
        assert "tries again at the next scheduled time" not in body
        assert "You won't get this message again" not in body

    def test_a_disabled_recurring_schedule_says_it_is_paused(self, failing_schedule):
        failing_schedule.is_enabled = False
        failing_schedule.save()

        emit_schedule_dispatch_failed(failing_schedule.pk)

        body = Notification.objects.get().body
        assert "It's paused, so it won't try again until it's enabled." in body
        assert "tries again at the next scheduled time" not in body

    def test_context_carries_what_the_renderers_show(self, failing_schedule):
        emit_schedule_dispatch_failed(failing_schedule.pk)

        ctx = Notification.objects.get().context
        assert ctx["status_tone"] == "failure"
        assert ctx["repo_ids"] == ["sfr/rt-daily-report"]
        assert ctx["reason"] == "Couldn't confirm the owner's write access to sfr/rt-daily-report"
        assert ctx["last_run"] == date_format(timezone.localtime(failing_schedule.last_run_at), "M j, H:i")

    def test_a_schedule_that_never_ran_says_so(self, failing_schedule):
        failing_schedule.last_run_at = None
        failing_schedule.save()

        emit_schedule_dispatch_failed(failing_schedule.pk)

        assert Notification.objects.get().context["last_run"] == "Never"
