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


def _single_repo_body(**overrides):
    body = {"repos": [{"repo_id": "group/project", "ref": None}], "prompt": "p"}
    body.update(overrides)
    return body


async def _make_task_row(task_id=None) -> AsyncMock:
    """Create a real DBTaskResult row so the Activity FK is satisfied, and return a mock that
    exposes the row's id."""
    tid = task_id or uuid.uuid4()
    await DBTaskResult.objects.acreate(
        id=tid,
        status="READY",
        task_path=run_job_task.module_path,
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after="9999-01-01T00:00:00Z",
        return_value={},
    )
    m = AsyncMock()
    m.id = tid
    return m


class _FakeActivity:
    """Stand-in for Activity returned from a mocked ``acreate_activity`` in tests that patch it."""

    def __init__(self, task_result_id):
        self.task_result_id = task_result_id


async def _fake_acreate_activity(**kwargs):
    return _FakeActivity(task_result_id=kwargs["task_result_id"])


def _patch_acreate():
    return patch("activity.services.acreate_activity", new_callable=AsyncMock, side_effect=_fake_acreate_activity)


# --- Authentication tests ---


@pytest.mark.django_db
async def test_submit_job_unauthenticated(client: TestAsyncClient):
    response = await client.post("/jobs", json=_single_repo_body(prompt="List all Python files"))
    assert response.status_code == 401


@pytest.mark.django_db
async def test_get_job_status_unauthenticated(client: TestAsyncClient):
    response = await client.get(f"/jobs/{uuid.uuid4()}")
    assert response.status_code == 401


# --- Validation tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_missing_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repos": [{"repo_id": "group/project", "ref": None}]})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_repos(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json={"repos": [], "prompt": "p"})
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_oversized_repos(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": f"o/r{i}", "ref": None} for i in range(21)], "prompt": "p"}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_repo_id(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "", "ref": None}], "prompt": "hello"}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_empty_prompt(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project", "ref": None}], "prompt": ""}
    )
    assert response.status_code == 422


# --- Submit job tests ---


@pytest.mark.django_db(transaction=True)
async def test_submit_job_success(authenticated_client: TestAsyncClient):
    task_id = uuid.uuid4()

    async def _aenq(**kwargs):
        return await _make_task_row(task_id)

    with patch("activity.services.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert "batch_id" in data
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["job_id"] == str(task_id)
    assert data["failed"] == []
    mock_task.aenqueue.assert_called_once()
    kwargs = mock_task.aenqueue.call_args.kwargs
    assert kwargs["repo_id"] == "group/project"
    assert kwargs["prompt"] == "List all files"
    assert kwargs["ref"] is None
    assert kwargs["use_max"] is False
    assert kwargs["thread_id"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_multi_repo(authenticated_client: TestAsyncClient):
    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("activity.services.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "o/a", "ref": None}, {"repo_id": "o/b", "ref": "dev"}], "prompt": "p"}
        )

    assert response.status_code == 202
    data = response.json()
    assert len(data["jobs"]) == 2


@pytest.mark.django_db(transaction=True)
async def test_submit_job_with_use_max(authenticated_client: TestAsyncClient):
    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("activity.services.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="Fix the bug", use_max=True))

    assert response.status_code == 202
    mock_task.aenqueue.assert_called_once()
    kwargs = mock_task.aenqueue.call_args.kwargs
    assert kwargs["repo_id"] == "group/project"
    assert kwargs["prompt"] == "Fix the bug"
    assert kwargs["ref"] is None
    assert kwargs["use_max"] is True
    assert kwargs["thread_id"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_use_max_to_activity(authenticated_client: TestAsyncClient):
    """Verify the submit endpoint threads ``use_max`` into ``acreate_activity``."""

    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="Fix the bug", use_max=True))

    assert response.status_code == 202
    assert mock_create.await_args.kwargs["use_max"] is True


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_notify_on_to_activity(authenticated_client: TestAsyncClient):
    """POST /jobs threads ``notify_on`` into ``acreate_activity``."""

    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(notify_on="always"))

    assert response.status_code == 202
    assert mock_create.await_args.kwargs["notify_on"] == "always"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_notify_on_optional(authenticated_client: TestAsyncClient):
    """Omitting ``notify_on`` is valid and forwards ``None`` (defer to user preference)."""

    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate() as mock_create:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body())

    assert response.status_code == 202
    assert mock_create.await_args.kwargs["notify_on"] is None


@pytest.mark.django_db(transaction=True)
async def test_submit_job_invalid_notify_on_returns_422(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post("/jobs", json=_single_repo_body(notify_on="bogus"))
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_all_enqueue_failures_reported(authenticated_client: TestAsyncClient):
    with patch("activity.services.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert data["jobs"] == []
    assert len(data["failed"]) == 1
    assert data["failed"][0]["repo_id"] == "group/project"


# --- Get job status tests (unchanged) ---


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
    db_result = await create_job_result(
        status="SUCCESSFUL", return_value={"response": "Here are the files...", "code_changes": False}
    )
    response = await authenticated_client.get(f"/jobs/{db_result.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(db_result.id)
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
