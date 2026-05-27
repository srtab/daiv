from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone

import pytest
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduledJob


@pytest.mark.django_db
class TestScheduledJobNotifyOn:
    def _make(self, user, **overrides):
        defaults = {
            "user": user,
            "name": "s",
            "prompt": "p",
            "frequency": "daily",
            "time": "12:00",
            "repos": [{"repo_id": "x/y", "ref": ""}],
        }
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
        defaults = {
            "user": user,
            "name": "s",
            "prompt": "p",
            "repos": [{"repo_id": "x/y", "ref": ""}],
            "frequency": "daily",
            "time": "12:00",
        }
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
            "repos": [{"repo_id": "x/y", "ref": "main"}],
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


@pytest.mark.django_db
class TestScheduledJobReposClean:
    def _build(self, user, **overrides):
        defaults = {
            "user": user,
            "name": "s",
            "prompt": "p",
            "frequency": Frequency.DAILY,
            "time": "12:00",
            "repos": [{"repo_id": "x/y", "ref": ""}],
        }
        defaults.update(overrides)
        return ScheduledJob(**defaults)

    def test_empty_repos_rejected(self, member_user):
        from django.core.exceptions import ValidationError

        s = self._build(member_user, repos=[])
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert "repos" in exc.value.error_dict

    def test_oversized_repos_rejected(self, member_user):
        from django.core.exceptions import ValidationError

        s = self._build(member_user, repos=[{"repo_id": f"r/{i}", "ref": ""} for i in range(21)])
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert "repos" in exc.value.error_dict

    def test_malformed_entry_rejected(self, member_user):
        from django.core.exceptions import ValidationError

        s = self._build(member_user, repos=[{"repo_id": "", "ref": "main"}])
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert "repos" in exc.value.error_dict

    def test_missing_ref_key_rejected(self, member_user):
        from django.core.exceptions import ValidationError

        s = self._build(member_user, repos=[{"repo_id": "x/y"}])
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert "repos" in exc.value.error_dict

    def test_duplicate_entries_rejected(self, member_user):
        from django.core.exceptions import ValidationError

        s = self._build(member_user, repos=[{"repo_id": "x/y", "ref": ""}, {"repo_id": "x/y", "ref": ""}])
        with pytest.raises(ValidationError) as exc:
            s.full_clean()
        assert "repos" in exc.value.error_dict

    def test_valid_repos_passes(self, member_user):
        s = self._build(member_user, repos=[{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])
        s.full_clean()  # no raise


def _future(seconds: int = 3600) -> datetime:
    return timezone.now() + timedelta(seconds=seconds)


@pytest.mark.django_db
class TestScheduledJobOnceValidation:
    def _make(self, user, **overrides):
        defaults = {
            "user": user,
            "name": "one-off",
            "prompt": "p",
            "repos": [{"repo_id": "x/y", "ref": ""}],
            "frequency": Frequency.ONCE,
            "run_at": _future(),
        }
        defaults.update(overrides)
        return ScheduledJob(**defaults)

    def test_once_with_future_run_at_is_valid(self, member_user):
        self._make(member_user).full_clean()  # no error

    def test_once_without_run_at_raises(self, member_user):
        with pytest.raises(ValidationError) as exc:
            self._make(member_user, run_at=None).full_clean()
        assert "run_at" in exc.value.message_dict

    def test_once_with_past_run_at_raises(self, member_user):
        past = timezone.now() - timedelta(minutes=5)
        with pytest.raises(ValidationError) as exc:
            self._make(member_user, run_at=past).full_clean()
        assert "run_at" in exc.value.message_dict

    def test_non_once_with_run_at_raises(self, member_user):
        with pytest.raises(ValidationError) as exc:
            self._make(member_user, frequency=Frequency.DAILY, time="09:00", run_at=_future()).full_clean()
        assert "run_at" in exc.value.message_dict

    def test_once_compute_next_run_uses_run_at(self, member_user):
        target = _future(7200)
        job = self._make(member_user, run_at=target)
        job.compute_next_run()
        assert job.next_run_at == target

    def test_once_get_effective_cron_raises(self, member_user):
        job = self._make(member_user)
        with pytest.raises(ValueError, match="ONCE"):
            job.get_effective_cron()


@pytest.mark.django_db
class TestScheduleTemplateOnceValidation:
    def test_template_accepts_once_without_run_at(self):
        from schedules.models import ScheduleTemplate

        ScheduleTemplate(name="t", prompt="p", frequency=Frequency.ONCE).full_clean()  # no error


@pytest.mark.django_db
class TestScheduledJobToScheduleKwargs:
    def test_returns_user_facing_fields(self, member_user):
        future = _future()
        job = ScheduledJob.objects.create(
            user=member_user,
            name="one-off",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=future,
            agent_model="openrouter:anthropic/claude-opus-4.6",
            agent_thinking_level="high",
            notify_on=NotifyOn.ALWAYS,
        )
        kwargs = job.to_schedule_kwargs()
        assert kwargs == {
            "name": "one-off",
            "prompt": "p",
            "repos": [{"repo_id": "x/y", "ref": ""}],
            "frequency": Frequency.ONCE,
            "cron_expression": "",
            "time": None,
            "run_at": future,
            "agent_model": "openrouter:anthropic/claude-opus-4.6",
            "agent_thinking_level": "high",
            "notify_on": NotifyOn.ALWAYS,
        }


@pytest.mark.django_db
class TestScheduledJobIsFiredOneOff:
    def test_false_before_fire(self, member_user):
        job = ScheduledJob(
            user=member_user,
            name="o",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=_future(),
        )
        assert job.is_fired_one_off is False

    def test_true_after_fire(self, member_user):
        job = ScheduledJob(
            user=member_user,
            name="o",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.ONCE,
            run_at=_future(),
            run_count=1,
        )
        assert job.is_fired_one_off is True

    def test_false_for_recurring_even_after_runs(self, member_user):
        job = ScheduledJob(
            user=member_user,
            name="d",
            prompt="p",
            repos=[{"repo_id": "x/y", "ref": ""}],
            frequency=Frequency.DAILY,
            time="09:00",
            run_count=42,
        )
        assert job.is_fired_one_off is False
