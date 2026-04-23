import pytest
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob


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


@pytest.mark.django_db
class TestScheduledJobRepos:
    def _make(self, user, **overrides):
        defaults = {
            "user": user,
            "name": "s",
            "prompt": "p",
            "repo_id": "x/y",
            "ref": "main",
            "frequency": Frequency.DAILY,
            "time": "12:00",
        }
        defaults.update(overrides)
        return ScheduledJob.objects.create(**defaults)

    def test_repos_field_accepts_list_of_dicts(self, member_user):
        s = self._make(member_user, repos=[{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])
        s.refresh_from_db()
        assert s.repos == [{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}]

    def test_last_run_batch_id_defaults_to_none(self, member_user):
        s = self._make(member_user)
        assert s.last_run_batch_id is None
