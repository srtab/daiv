import uuid

from django.test import Client
from django.urls import reverse

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
from django_tasks_db.models import DBTaskResult

from accounts.models import User


@pytest.fixture
def user(db):
    return User.objects.create_user(username="alice", email="alice@test.com", password="testpass123")  # noqa: S106


@pytest.fixture
def logged_in_client(user):
    client = Client()
    client.force_login(user)
    return client


def _create_task_result(*, status="SUCCESSFUL", return_value=None):
    return DBTaskResult.objects.create(
        id=uuid.uuid4(),
        status=status,
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after="9999-01-01T00:00:00Z",
        return_value=return_value or {},
        exception_class_path="",
        traceback="",
    )


def _create_activity(*, status=ActivityStatus.SUCCESSFUL, task_result=None, **kwargs):
    defaults = {
        "trigger_type": TriggerType.SCHEDULE,
        "repo_id": "group/project",
        "ref": "main",
        "prompt": "Run a security audit",
        "status": status,
        "task_result": task_result,
    }
    defaults.update(kwargs)
    return Activity.objects.create(**defaults)


@pytest.mark.django_db
class TestActivityDownloadMarkdownView:
    def test_download_with_task_result(self, logged_in_client):
        tr = _create_task_result(return_value={"response": "# Security Report\n\nAll good.", "code_changes": False})
        activity = _create_activity(task_result=tr)

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 200
        assert response["Content-Type"] == "text/markdown; charset=utf-8"
        assert "attachment" in response["Content-Disposition"]
        assert ".md" in response["Content-Disposition"]

        body = response.content.decode()
        assert "---" in body
        assert "repository: group/project" in body
        assert "# Security Report" in body
        assert "All good." in body

    def test_download_with_result_summary_fallback(self, logged_in_client):
        activity = _create_activity(result_summary="Summary of the result")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 200
        body = response.content.decode()
        assert "Summary of the result" in body

    def test_download_includes_metadata(self, logged_in_client):
        activity = _create_activity(
            result_summary="Result content", ref="feature-branch", issue_iid=42, merge_request_iid=10
        )

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "ref: feature-branch" in body
        assert "issue: '#42'" in body
        assert "merge_request: '!10'" in body
        assert "trigger: Scheduled Job" in body

    def test_download_filename_format(self, logged_in_client):
        activity = _create_activity(result_summary="Content")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        disposition = response["Content-Disposition"]
        assert disposition.startswith('attachment; filename="daiv-group-project-')
        assert disposition.endswith('.md"')

    def test_task_result_response_takes_priority_over_result_summary(self, logged_in_client):
        tr = _create_task_result(return_value={"response": "Full response text", "code_changes": False})
        activity = _create_activity(task_result=tr, result_summary="Truncated summary")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "Full response text" in body
        assert "Truncated summary" not in body

    def test_legacy_return_value_without_response_falls_back_to_summary(self, logged_in_client):
        tr = _create_task_result(return_value={"code_changes": True})
        activity = _create_activity(task_result=tr, result_summary="Fallback summary")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        body = response.content.decode()
        assert "Fallback summary" in body

    def test_non_successful_activity_returns_404(self, logged_in_client):
        activity = _create_activity(status=ActivityStatus.FAILED, result_summary="error")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 404

    def test_successful_activity_without_result_returns_404(self, logged_in_client):
        activity = _create_activity(result_summary="")

        response = logged_in_client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 404

    def test_unauthenticated_redirects_to_login(self):
        activity = _create_activity(result_summary="Content")
        client = Client()

        response = client.get(reverse("activity_download_md", kwargs={"pk": activity.pk}))

        assert response.status_code == 302
        assert "/accounts/login/" in response.url
