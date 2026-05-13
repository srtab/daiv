import json

import pytest
from notifications.choices import NotifyOn

from accounts.models import User
from schedules.forms import ScheduledJobCreateForm


def _valid_data(**overrides):
    data = {
        "name": "s",
        "prompt": "p",
        "repos": json.dumps([{"repo_id": "x/y", "ref": ""}]),
        "frequency": "daily",
        "cron_expression": "",
        "time": "12:00",
        "use_max": False,
        "notify_on": NotifyOn.NEVER,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobCreateForm:
    def test_valid_single_repo(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["repos"] == [{"repo_id": "x/y", "ref": ""}]

    def test_valid_multi_repo(self, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(repos=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])),
            owner=member_user,
        )
        assert form.is_valid(), form.errors

    def test_valid_with_notify_always(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(notify_on=NotifyOn.ALWAYS), owner=member_user)
        assert form.is_valid(), form.errors

    def test_save_persists_repos(self, member_user):
        form = ScheduledJobCreateForm(
            data=_valid_data(repos=json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}])),
            owner=member_user,
        )
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        job.refresh_from_db()
        assert job.repos == [{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}]

    def test_rejects_empty_repos(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(repos="[]"), owner=member_user)
        assert not form.is_valid()
        assert "repos" in form.errors


@pytest.mark.django_db
class TestScheduledJobCreateFormSubscribers:
    def _sub_user(self, username="alice"):
        return User.objects.create_user(
            username=username,
            email=f"{username}@t.com",
            password="x",  # noqa: S106
        )

    def test_form_accepts_subscribers(self, member_user):
        alice = self._sub_user("alice")
        form = ScheduledJobCreateForm(data=_valid_data(subscribers=[alice.pk]), owner=member_user)
        assert form.is_valid(), form.errors
        job = form.save(commit=False)
        job.user = member_user
        job.save()
        form.save_m2m()
        assert list(job.subscribers.all()) == [alice]

    def test_owner_excluded_from_queryset(self, member_user):
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert member_user.pk not in qs_pks

    def test_inactive_users_excluded_from_queryset(self, member_user):
        inactive = self._sub_user("bob")
        inactive.is_active = False
        inactive.save()
        form = ScheduledJobCreateForm(owner=member_user)
        qs_pks = list(form.fields["subscribers"].queryset.values_list("pk", flat=True))
        assert inactive.pk not in qs_pks

    def test_submitting_owner_pk_in_subscribers_is_rejected(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(subscribers=[member_user.pk]), owner=member_user)
        assert not form.is_valid()
        assert "subscribers" in form.errors

    def test_accepts_empty_subscribers(self, member_user):
        form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user)
        assert form.is_valid(), form.errors
