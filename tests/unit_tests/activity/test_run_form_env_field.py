"""Tests for the sandbox_environment field on the agent-run form."""

from __future__ import annotations

import json
import uuid
from unittest import mock

from django.urls import reverse

import pytest
from activity.forms import AgentRunCreateForm
from activity.models import Activity
from django_tasks_db.models import DBTaskResult, get_date_max
from sandbox_envs.models import SandboxEnvironment, Scope


def _make_task_result(task_id: uuid.UUID) -> mock.Mock:
    DBTaskResult.objects.create(
        id=task_id,
        status="READY",
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return mock.Mock(id=task_id)


@pytest.mark.django_db
def test_form_offers_user_and_global_envs(member_client, member_user):
    """GET request provides caller's USER envs + all GLOBAL envs via sandbox_envs context."""
    # A GLOBAL default is seeded by migration; add an additional non-default global to verify visibility.
    global_extra = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="GlobalExtra", base_image="g")
    user_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="my-dev-env", base_image="x")
    # Another user's env should NOT appear.
    from accounts.models import User

    other = User.objects.create_user(username="other", email="other@e.com", password="x")  # noqa: S106
    other_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="other-env", base_image="y")

    resp = member_client.get(reverse("runs:agent_run_new"))
    assert resp.status_code == 200
    # The env-picker renders names via escapejs (hyphens become -); check context instead of raw HTML.
    envs = list(resp.context["sandbox_envs"])
    env_ids = {e.id for e in envs}
    assert global_extra.id in env_ids
    assert user_env.id in env_ids
    assert other_env.id not in env_ids
    # The hidden input for form submission must still appear.
    assert "sandbox_environment" in resp.content.decode()


@pytest.mark.django_db
def test_form_init_filters_queryset_by_user(member_user):
    """The form __init__ should restrict the sandbox_environment queryset to user's envs + globals."""
    global_env = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="G", base_image="g")
    user_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="U", base_image="x")
    from accounts.models import User

    other = User.objects.create_user(username="other2", email="other2@e.com", password="x")  # noqa: S106
    other_env = SandboxEnvironment.objects.create(scope=Scope.USER, user=other, name="O", base_image="y")

    form = AgentRunCreateForm(user=member_user)
    qs = form.fields["sandbox_environment"].queryset
    ids = {e.id for e in qs}
    assert global_env.id in ids
    assert user_env.id in ids
    assert other_env.id not in ids


@pytest.mark.django_db(transaction=True)
def test_post_passes_sandbox_environment_id_to_submit(member_client, member_user):
    """Submitting the form with a sandbox_environment forwards the id to submit_batch_runs."""
    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=member_user, name="prod", base_image="x")

    from activity import services as _services

    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    with (
        mock.patch("activity.services.run_job_task") as m_task,
        mock.patch("activity.views.submit_batch_runs", wraps=_services.submit_batch_runs) as m_submit,
    ):
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        resp = member_client.post(
            reverse("runs:agent_run_new"),
            data={
                "prompt": "go",
                "repos": json.dumps([{"repo_id": "a/b", "ref": ""}]),
                "notify_on": "never",
                "sandbox_environment": str(env.id),
            },
        )

    assert resp.status_code == 302
    assert m_submit.call_args.kwargs["sandbox_environment_id"] == str(env.id)
    activity = Activity.objects.get(task_result_id=task_id)
    assert activity.sandbox_environment_id == env.id


@pytest.mark.django_db(transaction=True)
def test_post_without_env_passes_none(member_client):
    """Submitting without a selection forwards None (resolver picks GLOBAL default at runtime)."""
    from activity import services as _services

    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    with (
        mock.patch("activity.services.run_job_task") as m_task,
        mock.patch("activity.views.submit_batch_runs", wraps=_services.submit_batch_runs) as m_submit,
    ):
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        resp = member_client.post(
            reverse("runs:agent_run_new"),
            data={"prompt": "go", "repos": json.dumps([{"repo_id": "a/b", "ref": ""}]), "notify_on": "never"},
        )
    assert resp.status_code == 302
    assert m_submit.call_args.kwargs["sandbox_environment_id"] is None
