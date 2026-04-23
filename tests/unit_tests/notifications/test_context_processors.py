from unittest.mock import patch

from django.db import OperationalError
from django.test import RequestFactory

import pytest
from notifications.context_processors import unread_notification_count
from notifications.models import Notification


@pytest.fixture
def rf():
    return RequestFactory()


@pytest.mark.django_db
class TestUnreadNotificationCount:
    def test_returns_count_for_authenticated_user(self, rf, member_user):
        Notification.objects.create(recipient=member_user, event_type="e", subject="s", body="b", link_url="/")
        request = rf.get("/")
        request.user = member_user
        result = unread_notification_count(request)
        assert result == {"unread_count": 1}

    def test_returns_empty_for_anonymous_user(self, rf):
        from django.contrib.auth.models import AnonymousUser

        request = rf.get("/")
        request.user = AnonymousUser()
        assert unread_notification_count(request) == {}

    def test_returns_zero_on_database_error(self, rf, member_user):
        request = rf.get("/")
        request.user = member_user
        with patch.object(Notification.objects, "filter", side_effect=OperationalError("connection refused")):
            result = unread_notification_count(request)
        assert result == {"unread_count": 0}
