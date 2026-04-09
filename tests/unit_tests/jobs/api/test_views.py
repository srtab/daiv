import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django_tasks_db.models import DBTaskResult
from jobs.tasks import run_job_task
from ninja.testing import TestAsyncClient

from accounts.models import APIKey, User
from daiv.api import api


@pytest.fixture
def client():
    return TestAsyncClient(api)


@pytest.fixture
def api_key(db):
    user = User.objects.create_user(username="testuser", email="test@test.com", password="testpass")  # noqa: S106
    _, key = async_to_sync(APIKey.objects.create_key)(user=user, name="test-key")
    return key


@pytest.fixture
def authenticated_client(api_key):
    return TestAsyncClient(api, headers={"Authorization": f"Bearer {api_key}"})


# --- Authentication tests ---


@pytest.mark.django_db
async def test_submit_job_unauthenticated(client: TestAsyncClient):
    response = await client.post("/jobs", json={"repo_id": "group/project", "prompt": "List all Python files"})
    assert response.status_code == 401


@pytest.mark.django_db
async def test_get_job_status_unauthenticated(client: TestAsyncClient):
    response = await client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 401


# --- Validation tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_missing_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repo_id": "group/project"})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_repo_id(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repo_id": "", "prompt": "hello"})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repo_id": "group/project", "prompt": ""})
    assert response.status_code == 422


# --- Submit job tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_success(authenticated_client: TestAsyncClient):
    mock_result = AsyncMock()
    mock_result.id = str(uuid.uuid4())

    mock_activity = AsyncMock()
    mock_activity.id = uuid.uuid4()

    with (
        patch("jobs.api.views.run_job_task") as mock_task,
        patch("jobs.api.views.acreate_activity", new_callable=AsyncMock, return_value=mock_activity),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs", json={"repo_id": "group/project", "prompt": "List all files"}
        )

    assert response.status_code == 202
    data = response.json()
    assert data["job_id"] == mock_result.id
    assert data["activity_id"] == str(mock_activity.id)
    mock_task.aenqueue.assert_called_once_with(
        repo_id="group/project", prompt="List all files", ref=None, use_max=False
    )


@pytest.mark.django_db(transaction=True)
async def test_submit_job_with_use_max(authenticated_client: TestAsyncClient):
    mock_result = AsyncMock()
    mock_result.id = str(uuid.uuid4())

    mock_activity = AsyncMock()
    mock_activity.id = uuid.uuid4()

    with (
        patch("jobs.api.views.run_job_task") as mock_task,
        patch("jobs.api.views.acreate_activity", new_callable=AsyncMock, return_value=mock_activity),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs", json={"repo_id": "group/project", "prompt": "Fix the bug", "use_max": True}
        )

    assert response.status_code == 202
    mock_task.aenqueue.assert_called_once_with(repo_id="group/project", prompt="Fix the bug", ref=None, use_max=True)


@pytest.mark.django_db(transaction=True)
async def test_submit_job_enqueue_failure(authenticated_client: TestAsyncClient):
    with patch("jobs.api.views.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        response = await authenticated_client.post(
            "/jobs", json={"repo_id": "group/project", "prompt": "List all files"}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Failed to submit job. Please try again later."


# --- Get job status tests ---


@pytest.fixture
def create_job_result(db):
    async def _create(status="SUCCESSFUL", return_value=None, exception_class_path=""):
        return await DBTaskResult.objects.acreate(
            id=uuid.uuid4(),
            status=status,
            task_path=run_job_task.module_path,
            args_kwargs={"args": [], "kwargs": {}},
            queue_name="default",
            backend_name="default",
            run_after="9999-01-01T00:00:00Z",
            return_value=return_value,
            exception_class_path=exception_class_path,
            traceback="",
        )

    return _create


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_successful(authenticated_client: TestAsyncClient, create_job_result):
    db_result = await create_job_result(status="SUCCESSFUL", return_value="Here are the files...")
    response = await authenticated_client.get(f"/jobs/{db_result.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(db_result.id)
    assert data["activity_id"] == ""
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Here are the files..."
    assert data["error"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed(authenticated_client: TestAsyncClient, create_job_result):
    db_result = await create_job_result(status="FAILED", exception_class_path="builtins.RuntimeError")
    response = await authenticated_client.get(f"/jobs/{db_result.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "FAILED"
    assert data["result"] is None
    assert data["error"] == "Job execution failed"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found(authenticated_client: TestAsyncClient):
    response = await authenticated_client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_invalid_uuid(authenticated_client: TestAsyncClient):
    response = await authenticated_client.get("/jobs/not-a-uuid")
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wrong_task_path(authenticated_client: TestAsyncClient):
    """Ensure job IDs from other task types (e.g. webhooks) return 404."""
    db_result = await DBTaskResult.objects.acreate(
        id=uuid.uuid4(),
        status="SUCCESSFUL",
        task_path="codebase.tasks.address_issue_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after="9999-01-01T00:00:00Z",
        return_value=None,
        exception_class_path="",
        traceback="",
    )
    response = await authenticated_client.get(f"/jobs/{db_result.id}")
    assert response.status_code == 404
