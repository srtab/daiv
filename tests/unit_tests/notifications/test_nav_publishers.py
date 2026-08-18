"""Unread-count pokes: every write that changes unread state must reach the bell.

The publish lives on the model methods and ``create_notification`` rather than on the
views that call them, so a future caller can't leave the badge stale. Deferred to
commit in every case — a reader recounting before the row landed would send the old
number and then have nothing to correct it.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

import pytest
from notifications.choices import ChannelType
from notifications.models import Notification
from notifications.services import create_notification


def make_notification(user) -> Notification:
    return Notification.objects.create(
        recipient=user, event_type="schedule.finished", subject="n", body="b", link_url="/"
    )


@pytest.mark.django_db
class TestUnreadPokes:
    def test_creating_a_notification_pokes_its_recipient(self, member_user):
        with patch("core.ui_events.publisher.notifications_changed") as publish:
            with TestCase.captureOnCommitCallbacks(execute=False) as callbacks:
                create_notification(
                    recipient=member_user,
                    event_type="schedule.finished",
                    source_type="sessions.Run",
                    source_id="abc",
                    subject="Subject",
                    body="Body",
                    link_url="/x/",
                    channels=[ChannelType.EMAIL],
                )
            publish.assert_not_called()
            for callback in callbacks:
                callback()
        publish.assert_called_once_with(member_user.pk)

    def test_marking_one_read_pokes(self, member_user):
        notification = make_notification(member_user)
        with (
            patch("core.ui_events.publisher.notifications_changed") as publish,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            notification.mark_as_read()
        publish.assert_called_once_with(member_user.pk)

    def test_marking_an_already_read_one_does_not_poke(self, member_user):
        notification = make_notification(member_user)
        notification.mark_as_read()
        with (
            patch("core.ui_events.publisher.notifications_changed") as publish,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            notification.mark_as_read()
        publish.assert_not_called()

    def test_marking_all_read_pokes(self, member_user):
        """The bulk ``.update()`` fires no ``post_save``, and this is the path the bell
        dropdown takes on open — the badge clearing depends on it."""
        make_notification(member_user)
        with (
            patch("core.ui_events.publisher.notifications_changed") as publish,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            Notification.mark_all_read_for(member_user)
        publish.assert_called_once_with(member_user.pk)

    def test_marking_all_read_with_nothing_unread_does_not_poke(self, member_user):
        with (
            patch("core.ui_events.publisher.notifications_changed") as publish,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            Notification.mark_all_read_for(member_user)
        publish.assert_not_called()

    def test_opening_the_bell_dropdown_pokes(self, member_client, member_user):
        make_notification(member_user)
        with (
            patch("core.ui_events.publisher.notifications_changed") as publish,
            TestCase.captureOnCommitCallbacks(execute=True),
        ):
            response = member_client.get("/dashboard/notifications/bell/")
        assert response.status_code == 200
        publish.assert_called_once_with(member_user.pk)
