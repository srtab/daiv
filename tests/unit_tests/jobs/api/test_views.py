import uuid
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django_tasks_db.models import DBTaskResult
from jobs.tasks import run_job_task
from ninja.testing import TestAsyncClient
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import APIKey, User
from core.models import Provider, ProviderType
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


@pytest.fixture
def openrouter_provider(db):
    Provider.objects.filter(slug="openrouter").delete()
    return Provider.objects.create(
        slug="openrouter", provider_type=ProviderType.OPENROUTER, api_key="sk-test", is_enabled=True
    )


def _single_repo_body(**overrides):
    body = {"repos": [{"repo_id": "group/project", "ref": None}], "prompt": "p"}
    body.update(overrides)
    return body


async def _make_task_row(task_id=None) -> AsyncMock:
    """Create a real DBTaskResult row so the Run FK is satisfied, and return a mock that
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


class _FakeRun:
    """Stand-in for Run returned from a mocked ``acreate_run`` in tests that patch it."""

    def __init__(self, task_result_id):
        self.id = uuid.uuid4()
        self.pk = self.id
        self.task_result_id = task_result_id
        self.session_id = str(uuid.uuid4())
        self.status = RunStatus.READY
        self.started_at = None
        self.finished_at = None
        # Tests bypass the real ORM via ``_patch_acreate_run``; provide an async no-op
        # for the post-acreate ``run.asave(update_fields=...)`` call.
        self.asave = AsyncMock(return_value=None)


async def _fake_acreate_run(**kwargs):
    return _FakeRun(task_result_id=kwargs.get("task_result_id"))


def _patch_acreate_run():
    return patch("sessions.services.acreate_run", new_callable=AsyncMock, side_effect=_fake_acreate_run)


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

    with patch("sessions.services.run_job_task") as mock_task, _patch_acreate_run():
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

    with patch("sessions.services.run_job_task") as mock_task:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs", json={"repos": [{"repo_id": "o/a", "ref": None}, {"repo_id": "o/b", "ref": "dev"}], "prompt": "p"}
        )

    assert response.status_code == 202
    data = response.json()
    assert len(data["jobs"]) == 2


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_unknown_provider(authenticated_client: TestAsyncClient):
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "agent_model": "bogus:nope"}
    )
    assert response.status_code == 400
    assert "Unknown provider prefix" in response.json()["detail"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_invalid_thinking_level(authenticated_client: TestAsyncClient):
    """``agent_thinking_level`` outside the enum returns a clear 4xx instead of a silent
    accept. Ninja/Pydantic catches the enum mismatch at the protocol layer (422)."""
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "agent_thinking_level": "extreme"}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_rejects_extra_field(authenticated_client: TestAsyncClient):
    """``JobSubmitRequest`` is locked to ``extra='forbid'`` so a stale client still
    sending the dropped ``use_max`` field gets a 422 instead of a silent strip+202."""
    response = await authenticated_client.post(
        "/jobs", json={"repos": [{"repo_id": "group/project"}], "prompt": "Fix it", "use_max": True}
    )
    assert response.status_code == 422


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_agent_override(authenticated_client: TestAsyncClient, openrouter_provider):
    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("sessions.services.run_job_task") as mock_task, _patch_acreate_run() as mock_create:
        mock_task.aenqueue.side_effect = _aenq
        mock_task.module_path = run_job_task.module_path
        response = await authenticated_client.post(
            "/jobs",
            json={
                "repos": [{"repo_id": "group/project"}],
                "prompt": "Fix the bug",
                "agent_model": "openrouter:anthropic/claude-haiku-4.5",
                "agent_thinking_level": "low",
            },
        )

    assert response.status_code == 202
    assert mock_create.await_args.kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert mock_create.await_args.kwargs["agent_thinking_level"] == "low"
    enqueue_kwargs = mock_task.aenqueue.call_args.kwargs
    assert enqueue_kwargs["agent_model"] == "openrouter:anthropic/claude-haiku-4.5"
    assert enqueue_kwargs["agent_thinking_level"] == "low"


@pytest.mark.django_db(transaction=True)
async def test_submit_job_forwards_notify_on_to_run(authenticated_client: TestAsyncClient):
    """POST /jobs threads ``notify_on`` into ``acreate_run``."""

    async def _aenq(**kwargs):
        return await _make_task_row()

    with patch("sessions.services.run_job_task") as mock_task, _patch_acreate_run() as mock_create:
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

    with patch("sessions.services.run_job_task") as mock_task, _patch_acreate_run() as mock_create:
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
    with patch("sessions.services.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="List all files"))

    assert response.status_code == 202
    data = response.json()
    assert data["jobs"] == []
    assert len(data["failed"]) == 1
    assert data["failed"][0]["repo_id"] == "group/project"


# --- Get job status tests (Run-based) ---


async def _create_run_row(user, status="SUCCESSFUL", result_summary="", merge_request_web_url="", error_message=""):
    """Create a real Session+Run row for use in get_job_status tests."""
    thread_id = str(uuid.uuid4())
    session = await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.API_JOB, repo_id="group/project", user=user
    )
    return await Run.objects.acreate(
        session=session,
        trigger_type=SessionOrigin.API_JOB,
        repo_id="group/project",
        user=user,
        status=status,
        result_summary=result_summary,
        merge_request_web_url=merge_request_web_url,
        error_message=error_message,
    )


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_successful(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="SUCCESSFUL", result_summary="Here are the files...")
    response = await authenticated_client.get(f"/jobs/{run.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(run.id)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Here are the files..."
    assert data["error"] is None


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_failed(authenticated_client: TestAsyncClient):
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status="FAILED")
    response = await authenticated_client.get(f"/jobs/{run.id}")

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
async def test_get_job_status_other_user_run_returns_404(authenticated_client: TestAsyncClient):
    """Runs belonging to other users must not be accessible."""
    other = await User.objects.acreate_user(username="other2", email="other2@test.com", password="x")  # noqa: S106
    run = await _create_run_row(other, status="SUCCESSFUL", result_summary="secret")
    response = await authenticated_client.get(f"/jobs/{run.id}")
    assert response.status_code == 404


# --- Thread continuation tests ---


@pytest.mark.django_db(transaction=True)
class TestThreadContinuationAPI:
    async def test_response_includes_thread_id_and_status(self, authenticated_client):
        # New thread, no prior Run — should be READY
        with patch("sessions.services.run_job_task") as mock_task, _patch_acreate_run():
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
        session = await Session.objects.acreate(
            thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="a/b", user=other
        )
        await Run.objects.acreate(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", user=other)
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

    async def test_job_id_is_run_id(self, authenticated_client):
        created_runs: list = []

        async def capture_acreate_run(**kwargs):
            run = _FakeRun(task_result_id=kwargs.get("task_result_id"))
            created_runs.append(run)
            return run

        with (
            patch("sessions.services.run_job_task") as mock_task,
            patch("sessions.services.acreate_run", new_callable=AsyncMock, side_effect=capture_acreate_run),
            patch("sessions.services.generate_batch_title_task"),
        ):
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x"))

        body = response.json()
        assert len(created_runs) == 1
        assert body["jobs"][0]["job_id"] == str(created_runs[0].id)
        assert body["jobs"][0]["job_id"] != str(created_runs[0].task_result_id)

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
        """When an active sibling exists on the session, the second submission lands in QUEUED."""
        user = await User.objects.aget(username="testuser")
        thread = str(uuid.uuid4())
        session = await Session.objects.acreate(
            thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="group/project", user=user
        )
        await Run.objects.acreate(
            session=session,
            trigger_type=SessionOrigin.API_JOB,
            repo_id="group/project",
            status=RunStatus.RUNNING,
            user=user,
        )
        with patch("sessions.services.run_job_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=await _make_task_row())
            mock_task.module_path = run_job_task.module_path
            response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="x", thread_id=thread))
        assert response.status_code == 202
        body = response.json()
        assert body["jobs"][0]["status"] == "QUEUED"
        mock_task.aenqueue.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_queued_passes_through(authenticated_client: TestAsyncClient):
    """A QUEUED Run surfaces as ``status='QUEUED'`` in get_job_status (not 'PENDING')."""
    user = await User.objects.aget(username="testuser")
    run = await _create_run_row(user, status=RunStatus.QUEUED)
    response = await authenticated_client.get(f"/jobs/{run.id}")
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"


@pytest.mark.django_db(transaction=True)
async def test_thread_validation_uses_session_ownership(authenticated_client: TestAsyncClient):
    """Continuation of a thread whose session belongs to someone else -> 400 opaque error."""
    other = await User.objects.acreate_user(username="outsider", email="outsider@test.com", password="x")  # noqa: S106
    thread = str(uuid.uuid4())
    session = await Session.objects.acreate(thread_id=thread, origin=SessionOrigin.API_JOB, repo_id="a/b", user=other)
    await Run.objects.acreate(session=session, trigger_type=SessionOrigin.API_JOB, repo_id="a/b", user=other)
    body = _single_repo_body(prompt="x", thread_id=thread)
    response = await authenticated_client.post("/jobs", json=body)
    assert response.status_code == 400
    assert "thread_id not found" in response.json()["detail"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_denied_repo_returns_opaque_404(authenticated_client: TestAsyncClient):
    from unittest.mock import AsyncMock, patch

    from codebase.authorization import RepositoryAccessDenied

    with patch("jobs.api.views.aassert_can_run", new=AsyncMock(side_effect=RepositoryAccessDenied(["group/project"]))):
        response = await authenticated_client.post("/jobs", json=_single_repo_body(prompt="p"))

    assert response.status_code == 404
    assert response.json()["detail"] == "Repository not found or not accessible."
