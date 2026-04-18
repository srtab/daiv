import uuid
from unittest import mock

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404
from django.urls import reverse

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from django_tasks_db.models import DBTaskResult, get_date_max

from accounts.models import Role
from accounts.models import User as AccountUser


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


@pytest.mark.django_db
def test_get_blank_renders_empty_form(member_client):
    resp = member_client.get(reverse("runs:agent_run_new"))
    assert resp.status_code == 200
    assert resp.context["source_activity"] is None


@pytest.mark.django_db
def test_get_retry_prefills_fields(member_client, member_user):
    source = Activity.objects.create(
        user=member_user,
        status=ActivityStatus.SUCCESSFUL,
        trigger_type=TriggerType.API_JOB,
        repo_id="a/b",
        ref="develop",
        prompt="P",
        use_max=True,
    )
    resp = member_client.get(reverse("runs:agent_run_new") + f"?from={source.pk}")
    assert resp.status_code == 200
    assert resp.context["form"].initial == {"prompt": "P", "repo_id": "a/b", "ref": "develop", "use_max": True}
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
def test_post_valid_submits_and_redirects(member_client):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    with mock.patch("activity.services.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        resp = member_client.post(
            reverse("runs:agent_run_new"), data={"prompt": "go", "repo_id": "acme/repo", "ref": "", "use_max": "on"}
        )
    assert resp.status_code == 302
    created = Activity.objects.get(task_result_id=task_id)
    assert resp["Location"] == reverse("activity_detail", args=[created.pk])
    assert created.trigger_type == TriggerType.UI_JOB
    assert created.use_max is True


@pytest.mark.django_db
def test_get_retry_invalid_uuid_returns_404(member_client):
    resp = member_client.get(reverse("runs:agent_run_new") + "?from=not-a-uuid")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_post_submit_failure_rerenders_with_error(member_client, monkeypatch, caplog):
    def _boom(**kwargs):
        raise RuntimeError("broker is down")

    monkeypatch.setattr("activity.views.submit_ui_run", _boom)
    with caplog.at_level("ERROR", logger="daiv.activity"):
        resp = member_client.post(reverse("runs:agent_run_new"), data={"prompt": "go", "repo_id": "acme/repo"})
    assert resp.status_code == 200
    assert "Failed to submit" in resp.content.decode()

    # Operators need the traceback AND enough context (repo_id, ref) to triage the
    # failure without the user's prompt text; assert both are preserved on the log record.
    [record] = [r for r in caplog.records if r.name == "daiv.activity" and r.levelname == "ERROR"]
    assert record.exc_info is not None
    assert record.repo_id == "acme/repo"


@pytest.mark.django_db
@pytest.mark.parametrize("exc", [Http404, PermissionDenied, SuspiciousOperation])
def test_post_django_control_flow_exceptions_propagate(member_client, monkeypatch, exc):
    def _boom(**kwargs):
        raise exc("boom")

    monkeypatch.setattr("activity.views.submit_ui_run", _boom)
    resp = member_client.post(reverse("runs:agent_run_new"), data={"prompt": "go", "repo_id": "acme/repo"})
    # Django middleware renders these as 404/403/400 — not swallowed as "submit failed".
    assert resp.status_code in {400, 403, 404}
