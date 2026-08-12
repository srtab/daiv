"""Story 2.3 AC8 — RUN_FEED rows are carved out of the bell.

The bell's unread count, dropdown, and list page exclude RUN_FEED rows, and the bell's on-open
mark-all-read never marks a RUN_FEED row read — so Feed rows never inflate the bell badge and
opening the bell never clears the Feed's independent seen-state (load-bearing for Story 2.4).
"""

import uuid

from django.test import RequestFactory

import pytest
from notifications.choices import EventType
from notifications.context_processors import unread_notification_count
from notifications.models import Notification


def _feed_row(user):
    return Notification.objects.create(
        recipient=user,
        event_type=EventType.RUN_FEED,
        source_type="sessions.Run",
        source_id=str(uuid.uuid4()),
        subject="nightly",
        body="",
        link_url="/dashboard/sessions/abc/",
    )


def _bell_row(user, subject="bell"):
    return Notification.objects.create(
        recipient=user, event_type=EventType.SCHEDULE_FINISHED, subject=subject, body="b", link_url="/"
    )


@pytest.mark.django_db
class TestFeedBellCarveOut:
    def test_unread_count_excludes_feed_rows(self, member_user):
        _bell_row(member_user)
        _feed_row(member_user)
        _feed_row(member_user)
        request = RequestFactory().get("/")
        request.user = member_user
        assert unread_notification_count(request) == {"unread_count": 1}

    def test_feed_row_absent_from_bell_dropdown(self, member_client, member_user):
        _bell_row(member_user, subject="BELL-EVENT")
        feed = _feed_row(member_user)
        response = member_client.get("/dashboard/notifications/bell/")
        content = response.content.decode()
        assert "BELL-EVENT" in content
        assert feed.subject not in content or "nightly" not in content

    def test_feed_row_absent_from_notifications_list(self, member_client, member_user):
        _bell_row(member_user, subject="BELL-EVENT")
        _feed_row(member_user)
        response = member_client.get("/dashboard/notifications/")
        content = response.content.decode()
        assert "BELL-EVENT" in content
        assert "nightly" not in content

    def test_bell_open_mark_all_read_leaves_feed_unread(self, member_client, member_user):
        _bell_row(member_user)
        feed = _feed_row(member_user)
        member_client.get("/dashboard/notifications/bell/")
        # Bell rows are marked read on open; the Feed row's seen-state is untouched.
        assert Notification.objects.filter(recipient=member_user, read_at__isnull=True).count() == 1
        feed.refresh_from_db()
        assert feed.read_at is None

    def test_mark_all_read_view_leaves_feed_unread(self, member_client, member_user):
        _bell_row(member_user)
        feed = _feed_row(member_user)
        member_client.post("/dashboard/notifications/read-all/")
        feed.refresh_from_db()
        assert feed.read_at is None
        # The bell row WAS marked read.
        assert (
            Notification.objects.filter(
                recipient=member_user, event_type=EventType.SCHEDULE_FINISHED, read_at__isnull=False
            ).count()
            == 1
        )
