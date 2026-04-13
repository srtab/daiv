from django.core.exceptions import ValidationError

import pytest
from notifications.choices import NotifyOn

from schedules.models import ScheduledJob


@pytest.mark.django_db
class TestScheduledJobNotificationFields:
    def _make(self, user, **overrides):
        defaults = {"user": user, "name": "s", "prompt": "p", "repo_id": "x/y", "frequency": "daily", "time": "12:00"}
        defaults.update(overrides)
        return ScheduledJob(**defaults)

    def test_defaults(self, member_user):
        s = self._make(member_user)
        s.full_clean()
        assert s.notify_on == NotifyOn.NEVER
        assert s.notify_channels == []

    def test_notify_channels_validates_known_types(self, member_user):
        s = self._make(member_user, notify_on=NotifyOn.ALWAYS, notify_channels=["bogus"])
        with pytest.raises(ValidationError) as exc_info:
            s.full_clean()
        assert "bogus" in str(exc_info.value)

    def test_notify_on_without_channels_is_invalid(self, member_user):
        s = self._make(member_user, notify_on=NotifyOn.ALWAYS, notify_channels=[])
        with pytest.raises(ValidationError) as exc_info:
            s.full_clean()
        assert "notify_channels" in str(exc_info.value)

    def test_never_allows_empty_channels(self, member_user):
        s = self._make(member_user, notify_on=NotifyOn.NEVER, notify_channels=[])
        s.full_clean()  # no error
