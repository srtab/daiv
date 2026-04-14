import pytest
from notifications.choices import NotifyOn

from schedules.models import ScheduledJob


@pytest.mark.django_db
class TestScheduledJobNotifyOn:
    def _make(self, user, **overrides):
        defaults = {"user": user, "name": "s", "prompt": "p", "repo_id": "x/y", "frequency": "daily", "time": "12:00"}
        defaults.update(overrides)
        return ScheduledJob(**defaults)

    def test_defaults_to_never(self, member_user):
        s = self._make(member_user)
        s.full_clean()
        assert s.notify_on == NotifyOn.NEVER

    def test_accepts_always(self, member_user):
        s = self._make(member_user, notify_on=NotifyOn.ALWAYS)
        s.full_clean()  # no error
