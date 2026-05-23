import uuid
from unittest.mock import AsyncMock, patch

import pytest
from activity.models import Activity, ActivityStatus, TriggerType
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
        self.id = uuid.uuid4()
        self.task_result_id = task_result_id
        self.thread_id = str(uuid.uuid4())
        self.status = ActivityStatus.READY
        # Tests bypass the real ORM via ``_patch_acreate``; provide an async no-op
        # for the post-acreate ``activity.asave(update_fields=...)`` call.
        self.asave = AsyncMock(return_value=None)


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

    with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert "batch_id" in data
    assert len(data["jobs"]) == 1
    # Verify it's a valid UUID
    assert uuid.UUID(data["jobs"][0]["job_id"])
    assert data["jobs"][0]["thread_id"]
    assert data["jobs"][0]["status"] in ("READY", "QUEUED")
    assert data["failed"] == []
    mock_task.aenqueue.assert_called_once()
    kwargs = mock_task.aenqueue.call_args.kwargs
    assert kwargs["repo_id"] == "group/project"
    assert kwargs["prompt"] == "List all files"
    assert kwargs["ref"] is None
    assert kwargs["agent_model"] is None
    assert kwargs["agent_thinking_level"] is None
    assert "use_max" not in kwargs
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


# NOTE: use_max-based API tests removed; Task 8 adds new tests for agent_model/agent_thinking_level.


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


# --- Get job status tests (Activity-based) ---


async def _create_activity_row(
    user, status="SUCCESSFUL", result_summary="", merge_request_web_url="", error_message=""
):
    """Create a real Activity row for use in get_job_status tests."""
    return await Activity.objects.acreate(
        trigger_type=TriggerType.API_JOB,
        repo_id="group/project",
        user=user,
        status=status,
        thread_id=str(uuid.uuid4()),
        result_summary=result_summary,
        merge_request_web_url=merge_request_web_url,
        error_message=error_message,
    )


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_successful(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    activity = await _create_activity_row(user, status="SUCCESSFUL", result_summary="Here are the files...")
    response = await authenticated_client.get(f"/jobs/{activity.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(activity.id)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Here are the files..."
    assert data["error"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    activity = await _create_activity_row(user, status="FAILED")
    response = await authenticated_client.get(f"/jobs/{activity.id}")

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
async def test_get_job_status_other_user_activity_returns_404(authenticated_client: TestAsyncClient):
    """Activities belonging to other users must not be accessible."""
    other = await User.objects.acreate_user(username="other2", email="other2@test.com", password="x")  # noqa: S106
    activity = await _create_activity_row(other, status="SUCCESSFUL", result_summary="secret")
    response = await authenticated_client.get(f"/jobs/{activity.id}")
    assert response.status_code == 404


# --- Thread continuation tests ---


@pytest.mark.django_db(transaction=True)
class TestThreadContinuationAPI:
    async def test_response_includes_thread_id_and_status(self, authenticated_client):
        # New thread, no prior Activity — should be READY
        with patch("activity.services.run_job_task") as mock_task, _patch_acreate():
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))
        assert response.status_code == 202
        body = response.json()
        assert body["jobs"][0]["thread_id"]
        assert body["jobs"][0]["status"] == "READY"

    async def test_continuation_with_unknown_thread_id_rejects(self, authenticated_client):
        body = _single_repo_body(prompt="x", thread_id=str(uuid.uuid4()))
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "thread_id not found" in response.json()["detail"]

    async def test_continuation_with_other_user_thread_id_rejects(self, authenticated_client, db):
        other = await User.objects.acreate_user(username="other", email="o@t.com", password="x")  # noqa: S106
        thread = str(uuid.uuid4())
        await Activity.objects.acreate(trigger_type=TriggerType.API_JOB, repo_id="a/b", thread_id=thread, user=other)
        body = _single_repo_body(prompt="x", thread_id=thread)
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "thread_id not found" in response.json()["detail"]

    async def test_continuation_with_multi_repo_rejects(self, authenticated_client):
        body = {
            "repos": [{"repo_id": "a/b", "ref": None}, {"repo_id": "c/d", "ref": None}],
            "prompt": "x",
            "thread_id": str(uuid.uuid4()),
        }
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 400
        assert "exactly one repo" in response.json()["detail"]

    async def test_job_id_is_activity_id(self, authenticated_client):
        created_activities: list = []

        async def capture_acreate(**kwargs):
            activity = _FakeActivity(task_result_id=kwargs["task_result_id"])
            created_activities.append(activity)
            return activity

        with (
            patch("activity.services.run_job_task") as mock_task,
            patch("activity.services.acreate_activity", new_callable=AsyncMock, side_effect=capture_acreate),
            patch("activity.services.generate_batch_title_task"),
        ):
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))

        body = response.json()
        assert len(created_activities) == 1
        assert body["jobs"][0]["job_id"] == str(created_activities[0].id)
        assert body["jobs"][0]["job_id"] != str(created_activities[0].task_result_id)

    async def test_empty_thread_id_rejected_at_schema(self, authenticated_client):
        """An empty-string thread_id is malformed at the protocol layer (422, not 400)."""
        body = _single_repo_body(prompt="x", thread_id="")
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 422

    async def test_malformed_thread_id_rejected_at_schema(self, authenticated_client):
        """A non-UUID thread_id is rejected by the schema, not silently mapped to 'not found'."""
        body = _single_repo_body(prompt="x", thread_id="not-a-uuid")
        response = await authenticated_client.post("/jobs", json=body)
        assert response.status_code == 422

    async def test_continuation_creates_queued_when_sibling_running(self, authenticated_client):
        """An IntegrityError fallback path: when an active sibling exists on the thread,
        the second submission lands in QUEUED with no enqueue."""
        user = await User.objects.aget(username="testuser")
        thread = str(uuid.uuid4())
        await Activity.objects.acreate(
            trigger_type=TriggerType.API_JOB,
            repo_id="group/project",
            thread_id=thread,
            status=ActivityStatus.RUNNING,
            user=user,
        )
        with patch("activity.services.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x", thread_id=thread))
        assert response.status_code == 202
        body = response.json()
        assert body["jobs"][0]["status"] == "QUEUED"
        mock_task.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_queued_passes_through(authenticated_client: TestAsyncClient):
    """A QUEUED Activity surfaces as ``status='QUEUED'`` in get_job_status (not 'PENDING')."""
    user = await User.objects.aget(username="testuser")
    activity = await _create_activity_row(user, status=ActivityStatus.QUEUED)
    response = await authenticated_client.get(f"/jobs/{activity.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
