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


@pytest.mark.django_db
class TestScheduledJobSubscribers:
    def _make(self, user, **overrides):
        defaults = {"user": user, "name": "s", "prompt": "p", "repo_id": "x/y", "frequency": "daily", "time": "12:00"}
        defaults.update(overrides)
        return ScheduledJob.objects.create(**defaults)

    def test_subscribers_empty_by_default(self, member_user):
        job = self._make(member_user)
        assert list(job.subscribers.all()) == []

    def test_add_and_remove_subscribers(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        assert list(job.subscribers.all()) == [admin_user]
        job.subscribers.remove(admin_user)
        assert list(job.subscribers.all()) == []

    def test_deleting_subscriber_user_removes_membership(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        admin_user.delete()
        job.refresh_from_db()
        assert list(job.subscribers.all()) == []

    def test_reverse_accessor_subscribed_schedules(self, member_user, admin_user):
        job = self._make(member_user)
        job.subscribers.add(admin_user)
        assert list(admin_user.subscribed_schedules.all()) == [job]
