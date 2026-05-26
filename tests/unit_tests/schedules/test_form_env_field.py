"""Tests for the sandbox_environment field on the schedule create/update forms."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from notifications.choices import NotifyOn
from sandbox_envs.models import SandboxEnvironment, Scope

from schedules.forms import ScheduledJobCreateForm

if TYPE_CHECKING:
    from schedules.models import ScheduledJob


def _valid_data(**overrides):
    data = {
        "name": "s",
        "prompt": "p",
        "repos": json.dumps([{"repo_id": "x/y", "ref": ""}]),
        "frequency": "daily",
        "cron_expression": "",
        "time": "12:00",
        "notify_on": NotifyOn.NEVER,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_form_offers_sandbox_environment_field(member_user):
    """The schedule form exposes the sandbox_environment field, scoped to caller + globals."""
    user_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="dev", base_image="x")
    from accounts.models import User

    other = User.objects.create_user(username="other_s", email="other_s@e.com", password="x")  # noqa: S106
    other_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="other", base_image="y")

    form = ScheduledJobCreateForm(owner=member_user, user=member_user)
    qs = form.fields["sandbox_environment"].queryset
    ids = {e.id for e in qs}
    assert user_env.id in ids
    assert other_env.id not in ids


@pytest.mark.django_db
def test_form_save_persists_sandbox_environment(member_user):
    """Submitting the form with a sandbox_environment persists it on the ScheduledJob."""
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="prod", base_image="x")
    form = ScheduledJobCreateForm(
        data=_valid_data(sandbox_environment=str(env.id)), owner=member_user, user=member_user
    )
    assert form.is_valid(), form.errors
    job: ScheduledJob = form.save(commit=False)
    job.user = member_user
    job.save()
    form.save_m2m()
    job.refresh_from_db()
    assert job.sandbox_environment_id == env.id


@pytest.mark.django_db
def test_form_save_without_env_stores_null(member_user):
    """Omitting sandbox_environment leaves the FK null (runtime resolver picks default)."""
    form = ScheduledJobCreateForm(data=_valid_data(), owner=member_user, user=member_user)
    assert form.is_valid(), form.errors
    job: ScheduledJob = form.save(commit=False)
    job.user = member_user
    job.save()
    form.save_m2m()
    job.refresh_from_db()
    assert job.sandbox_environment_id is None
