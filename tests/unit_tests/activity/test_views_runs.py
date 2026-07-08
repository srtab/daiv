import json
import uuid
from unittest import mock

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404
from django.urls import reverse
from django.utils import timezone

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from allauth.socialaccount.models import SocialAccount
from django_tasks_db.models import DBTaskResult, get_date_max

from accounts.models import Role
from accounts.models import User as AccountUser
from codebase.base import RepoAccessLevel
from codebase.models import RepositoryAccess


def _grant_access(user, repo_id, level):
    SocialAccount.objects.get_or_create(user=user, provider="gitlab", defaults={"uid": f"uid-{user.pk}"})
    account = SocialAccount.objects.get(user=user, provider="gitlab")
    RepositoryAccess.objects.create(
        provider="gitlab",
        uid=account.uid,
        username=user.username,
        repo_id=repo_id,
        access_level=level,
        synced_at=timezone.now(),
    )


def _make_user(username: str) -> AccountUser:
    return AccountUser.objects.create_user(
        username=username,
        email=f"{username}@test.com",
        password="testpass123",  # noqa: S106
        role=Role.MEMBER,
    )


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


async def _amake_task_result(task_id: uuid.UUID) -> mock.Mock:
    await DBTaskResult.objects.acreate(
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


def _single_repo_post_data(repo_id="acme/repo", ref=""):
    return {"prompt": "go", "repos": json.dumps([{"repo_id": repo_id, "ref": ref}]), "notify_on": "never"}


@pytest.mark.django_db
def test_get_provides_sandbox_envs_in_context(member_client):
    resp = member_client.get(reverse("runs:agent_run_new"))
    assert resp.status_code == 200
    assert "sandbox_envs" in resp.context
    envs = list(resp.context["sandbox_envs"])
    # GLOBAL Default is seeded by migration — always present.
    assert any(e.scope == "global" and e.is_default for e in envs)
    assert resp.context["selected_sandbox_env_id"] == ""


@pytest.mark.django_db
def test_get_blank_renders_empty_form(member_client):
    resp = member_client.get(reverse("runs:agent_run_new"))
    assert resp.status_code == 200
    assert resp.context["source_activity"] is None


@pytest.mark.django_db
def test_get_retry_prefills_fields(member_client, member_user):
    _grant_access(member_user, "a/b", RepoAccessLevel.WRITE)
    source = Activity.objects.create(
        user=member_user,
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.API_JOB,
        repo_id="a/b",
        ref="develop",
        prompt="P",
        agent_model="openrouter:anthropic/claude-opus-4.6",
        agent_thinking_level="high",
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial == {
        "notify_on": member_user.notify_on_jobs,
        "prompt": "P",
        "repos": [{"repo_id": "a/b", "ref": "develop"}],
        "agent_model": "openrouter:anthropic/claude-opus-4.6",
        "agent_thinking_level": "high",
    }
    assert resp.context["source_activity"].pk == source.pk


@pytest.mark.django_db
@pytest.mark.parametrize("status", [ActivityStatus.READY, ActivityStatus.RUNNING])
def test_get_retry_non_terminal_returns_404(member_client, member_user, status):
    source = Activity.objects.create(user=member_user, status=status, trigger_type=TriggerType.API_JOB, repo_id="a/b")
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("trigger", [TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK])
def test_get_retry_webhook_returns_404(member_client, member_user, trigger):
    source = Activity.objects.create(
        user=member_user, status=ActivityStatus.SUCCESSFUL, trigger_type=trigger, repo_id="a/b"
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_get_retry_other_users_activity_returns_404(member_client):
    owner = _make_user("owner2")
    source = Activity.objects.create(
        user=owner, status=ActivityStatus.SUCCESSFUL, trigger_type=TriggerType.API_JOB, repo_id="a/b"
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_post_single_repo_redirects_to_activity_detail(member_client):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    with mock.patch("activity.services.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        resp = member_client.post(reverse("runs:agent_run_new"), data=_single_repo_post_data())
    assert resp.status_code == 302
    created = Activity.objects.get(task_result_id=task_id)
    assert resp["Location"] == reverse("activity_detail", args=[created.pk])
    assert created.trigger_type == TriggerType.UI_JOB
    assert created.use_max is False
    assert created.agent_model == ""
    assert created.agent_thinking_level == ""
    assert created.batch_id is not None


@pytest.mark.django_db(transaction=True)
def test_post_multi_repo_redirects_to_filtered_activity_list(member_client):
    async def _aenqueue(**kwargs):
        return await _amake_task_result(uuid.uuid4())

    with mock.patch("activity.services.run_job_task") as m_task:
        m_task.aenqueue = _aenqueue
        resp = member_client.post(
            reverse("runs:agent_run_new"),
            data={
                "prompt": "go",
                "repos": json.dumps([{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "main"}]),
                "notify_on": "never",
            },
        )
    assert resp.status_code == 302
    assert "batch=" in resp["Location"]
    activities = list(Activity.objects.all())
    assert len(activities) == 2
    assert len({a.batch_id for a in activities}) == 1


@pytest.mark.django_db
def test_get_retry_invalid_uuid_returns_404(member_client):
    resp = member_client.get(reverse("runs:agent_run_new") + "?from=not-a-uuid")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_post_submit_failure_rerenders_with_error(member_client, monkeypatch, caplog):
    def _boom(**kwargs):
        raise RuntimeError("broker is down")

    monkeypatch.setattr("activity.views.submit_batch_runs", _boom)
    with caplog.at_level("ERROR", logger="daiv.activity"):
        resp = member_client.post(reverse("runs:agent_run_new"), data=_single_repo_post_data())
    assert resp.status_code == 200
    assert "Failed to submit" in resp.content.decode()

    # Operators need the traceback AND enough context (repos list) to triage the
    # failure without the user's prompt text; assert both are preserved on the log record.
    [record] = [r for r in caplog.records if r.name == "daiv.activity" and r.levelname == "ERROR"]
    assert record.exc_info is not None
    assert record.repos == [{"repo_id": "acme/repo", "ref": ""}]


@pytest.mark.django_db
@pytest.mark.parametrize("exc", [Http404, PermissionDenied, SuspiciousOperation])
def test_post_django_control_flow_exceptions_propagate(member_client, monkeypatch, exc):
    def _boom(**kwargs):
        raise exc("boom")

    monkeypatch.setattr("activity.views.submit_batch_runs", _boom)
    resp = member_client.post(reverse("runs:agent_run_new"), data=_single_repo_post_data())
    # Django middleware renders these as 404/403/400 — not swallowed as "submit failed".
    assert resp.status_code in {400, 403, 404}


@pytest.mark.django_db
def test_post_invalid_agent_model_renders_field_error(member_client):
    """A malformed ``agent_model`` posted via raw form data must surface as a visible
    error on the page, not silently re-render with no message.
    """
    data = {**_single_repo_post_data(), "agent_model": "bogus:nope"}
    resp = member_client.post(reverse("runs:agent_run_new"), data=data)
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Unknown provider prefix" in body
