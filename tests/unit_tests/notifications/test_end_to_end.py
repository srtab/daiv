from django.core import mail
from django.test import TestCase

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished
from notifications.choices import NotifyOn
from notifications.models import Notification, NotificationDelivery
from notifications.tasks import _deliver_notification

from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
def test_schedule_failure_sends_email(member_user):
    # Arrange: user + schedule with on_failure notification on email
    schedule = ScheduledJob.objects.create(
        user=member_user,
        name="Nightly",
        prompt="p",
        repo_id="x/y",
        frequency=Frequency.DAILY,
        time="12:00",
        notify_on=NotifyOn.ON_FAILURE,
        notify_channels=["email"],
    )
    activity = Activity.objects.create(
        trigger_type=TriggerType.SCHEDULE,
        user=member_user,
        repo_id="x/y",
        scheduled_job=schedule,
        status=ActivityStatus.FAILED,
        error_message="Something broke.",
    )

    # Act: emit the signal (what task_finished would do on a real terminal transition)
    with TestCase.captureOnCommitCallbacks(execute=True):
        activity_finished.send(sender=Activity, activity=activity)

    # Assert: Notification + Delivery created
    notification = Notification.objects.get(recipient=member_user)
    assert notification.event_type == "schedule.finished"
    delivery = NotificationDelivery.objects.get(notification=notification)
    assert delivery.channel_type == "email"

    # Execute the queued delivery task synchronously
    _deliver_notification(delivery.id)

    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert len(mail.outbox) == 1
    assert "Nightly" in mail.outbox[0].subject
    assert "Something broke." in mail.outbox[0].body
