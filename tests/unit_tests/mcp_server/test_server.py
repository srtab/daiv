import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp_server.server import get_job_status, list_repositories, submit_job

from codebase.base import GitPlatform, Repository


@pytest.mark.django_db(transaction=True)
async def test_submit_job_success():
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    with patch("mcp_server.server.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        result = await submit_job(repo_id="group/project", prompt="Fix the bug")

    data = json.loads(result)
    assert "job_id" in data
    assert data["job_id"] == mock_result.id


@pytest.mark.django_db(transaction=True)
async def test_submit_job_passes_ref():
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    with patch("mcp_server.server.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        await submit_job(repo_id="group/project", prompt="Fix the bug", ref="feature-branch")
        mock_task.aenqueue.assert_called_once_with(repo_id="group/project", prompt="Fix the bug", ref="feature-branch")


@pytest.mark.django_db(transaction=True)
async def test_submit_job_failure():
    with patch("mcp_server.server.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        result = await submit_job(repo_id="group/project", prompt="Fix the bug")

    data = json.loads(result)
    assert "error" in data
    assert "group/project" in data["error"]


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_success():
    job_id = str(uuid.uuid4())
    mock_result = MagicMock()
    mock_result.id = job_id

    now = datetime.now(UTC)
    mock_db_result = MagicMock()
    mock_db_result.id = job_id
    mock_db_result.status = "SUCCESSFUL"
    mock_db_result.return_value = "All done"
    mock_db_result.enqueued_at = now
    mock_db_result.started_at = now
    mock_db_result.finished_at = now

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(return_value=mock_db_result)

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "All done"
    assert data["error"] is None
    assert data["job_id"] == job_id


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_failed():
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    now = datetime.now(UTC)
    mock_db_result = MagicMock()
    mock_db_result.status = "FAILED"
    mock_db_result.return_value = None
    mock_db_result.enqueued_at = now
    mock_db_result.started_at = now
    mock_db_result.finished_at = now

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(return_value=mock_db_result)

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["status"] == "FAILED"
    assert data["error"] == "Job execution failed."


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_polls_until_complete():
    """Test that wait=True polls multiple times until the job finishes."""
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    now = datetime.now(UTC)
    running_result = MagicMock()
    running_result.status = "RUNNING"

    finished_result = MagicMock()
    finished_result.status = "SUCCESSFUL"
    finished_result.return_value = "Done"
    finished_result.enqueued_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(side_effect=[running_result, running_result, finished_result])

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    assert mock_model.objects.aget.call_count == 3


@pytest.mark.django_db(transaction=True)
async def test_submit_job_wait_timeout():
    """Test that wait=True returns status when max poll duration is exceeded."""
    mock_result = MagicMock()
    mock_result.id = str(uuid.uuid4())

    now = datetime.now(UTC)
    running_result = MagicMock()
    running_result.status = "RUNNING"
    running_result.return_value = None
    running_result.enqueued_at = now
    running_result.started_at = now
    running_result.finished_at = None

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.MAX_POLL_DURATION", 4.0),
        patch("mcp_server.server.POLL_INTERVAL", 2.0),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(return_value=running_result)
        mock_model.DoesNotExist = Exception

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["status"] == "RUNNING"


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found():
    result = await get_job_status(job_id=str(uuid.uuid4()))
    data = json.loads(result)
    assert data["error"] == "Job not found."


async def test_get_job_status_invalid_uuid():
    result = await get_job_status(job_id="not-a-uuid")
    data = json.loads(result)
    assert data["error"] == "Invalid job_id format."


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_already_complete():
    """When wait=True but the job is already complete, return immediately."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    mock_db_result = MagicMock()
    mock_db_result.id = job_id
    mock_db_result.status = "SUCCESSFUL"
    mock_db_result.return_value = "Done"
    mock_db_result.enqueued_at = now
    mock_db_result.started_at = now
    mock_db_result.finished_at = now

    with patch("mcp_server.server.run_job_task") as mock_task, patch("mcp_server.server.DBTaskResult") as mock_model:
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.objects.aget = AsyncMock(return_value=mock_db_result)
        mock_model.DoesNotExist = Exception

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    assert data["result"] == "Done"
    # Should not have polled — only the initial fetch
    assert mock_model.objects.aget.call_count == 1


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_polls_until_complete():
    """When wait=True and the job is still running, poll until complete."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    running_result = MagicMock()
    running_result.id = job_id
    running_result.status = "RUNNING"

    finished_result = MagicMock()
    finished_result.id = job_id
    finished_result.status = "SUCCESSFUL"
    finished_result.return_value = "Done"
    finished_result.enqueued_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.module_path = "jobs.tasks.run_job_task"
        # First call is the initial fetch (running), then polling finds it finished
        mock_model.objects.aget = AsyncMock(side_effect=[running_result, finished_result])
        mock_model.DoesNotExist = Exception

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"
    # 1 initial fetch + 1 poll
    assert mock_model.objects.aget.call_count == 2


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_wait_not_found_then_appears():
    """When wait=True and the job doesn't exist yet, poll until it appears."""
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    finished_result = MagicMock()
    finished_result.id = job_id
    finished_result.status = "SUCCESSFUL"
    finished_result.return_value = "Done"
    finished_result.enqueued_at = now
    finished_result.started_at = now
    finished_result.finished_at = now

    class _DoesNotExistError(Exception):
        pass

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        # Initial fetch raises DoesNotExist, then poll finds it
        mock_model.objects.aget = AsyncMock(side_effect=[_DoesNotExistError, _DoesNotExistError, finished_result])

        result = await get_job_status(job_id=job_id, wait=True)

    data = json.loads(result)
    assert data["status"] == "SUCCESSFUL"


@pytest.mark.django_db(transaction=True)
async def test_poll_job_db_exception():
    """DB error during polling returns an error response."""
    job_id = str(uuid.uuid4())
    mock_result = MagicMock()
    mock_result.id = job_id

    class _DoesNotExistError(Exception):
        pass

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert "error" in data
    assert "Failed to retrieve job status" in data["error"]


@pytest.mark.django_db(transaction=True)
async def test_poll_job_timeout_never_found():
    """When the job never appears in DB during polling, return 'Job not found.'."""
    job_id = str(uuid.uuid4())
    mock_result = MagicMock()
    mock_result.id = job_id

    class _DoesNotExistError(Exception):
        pass

    with (
        patch("mcp_server.server.run_job_task") as mock_task,
        patch("mcp_server.server.DBTaskResult") as mock_model,
        patch("mcp_server.server.asyncio.sleep", new_callable=AsyncMock),
        patch("mcp_server.server.MAX_POLL_DURATION", 4.0),
        patch("mcp_server.server.POLL_INTERVAL", 2.0),
    ):
        mock_task.aenqueue = AsyncMock(return_value=mock_result)
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=_DoesNotExistError)

        result = await submit_job(repo_id="group/project", prompt="Fix the bug", wait=True)

    data = json.loads(result)
    assert data["error"] == "Job not found."


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_db_exception():
    """Generic DB exception in get_job_status returns error response."""
    job_id = str(uuid.uuid4())

    class _DoesNotExistError(Exception):
        pass

    with patch("mcp_server.server.run_job_task") as mock_task, patch("mcp_server.server.DBTaskResult") as mock_model:
        mock_task.module_path = "jobs.tasks.run_job_task"
        mock_model.DoesNotExist = _DoesNotExistError
        mock_model.objects.aget = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        result = await get_job_status(job_id=job_id)

    data = json.loads(result)
    assert "error" in data
    assert "Failed to retrieve job status" in data["error"]


async def test_list_repositories_success(mock_repo_client):
    mock_repo_client.list_repositories.return_value = [
        Repository(
            pk=1,
            slug="group/project",
            name="project",
            default_branch="main",
            git_platform=GitPlatform.GITLAB,
            clone_url="https://gitlab.com/group/project.git",
            html_url="https://gitlab.com/group/project",
            topics=["python"],
        )
    ]

    result = await list_repositories()
    data = json.loads(result)

    assert "repositories" in data
    assert len(data["repositories"]) == 1
    assert data["repositories"][0]["repo_id"] == "group/project"
    assert data["repositories"][0]["name"] == "project"
    assert data["repositories"][0]["default_branch"] == "main"
    assert data["repositories"][0]["topics"] == ["python"]
    assert data["repositories"][0]["url"] == "https://gitlab.com/group/project"


async def test_list_repositories_empty(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    result = await list_repositories()
    data = json.loads(result)

    assert data["repositories"] == []


async def test_list_repositories_with_search(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    await list_repositories(search="test")
    mock_repo_client.list_repositories.assert_called_with(search="test", topics=None)


async def test_list_repositories_with_topics(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    await list_repositories(topics=["python", "django"])
    mock_repo_client.list_repositories.assert_called_with(search=None, topics=["python", "django"])


async def test_list_repositories_not_implemented_fallback(mock_repo_client):
    mock_repo_client.list_repositories.side_effect = [
        NotImplementedError("Search not supported"),
        [
            Repository(
                pk=1,
                slug="group/project",
                name="project",
                default_branch="main",
                git_platform=GitPlatform.GITLAB,
                clone_url="https://gitlab.com/group/project.git",
                html_url="https://gitlab.com/group/project",
                topics=[],
            )
        ],
    ]

    result = await list_repositories(search="test", topics=["python"])
    data = json.loads(result)

    assert len(data["repositories"]) == 1
    # Verify the retry call dropped the search parameter
    assert mock_repo_client.list_repositories.call_count == 2
    mock_repo_client.list_repositories.assert_called_with(topics=["python"])


async def test_list_repositories_generic_exception(mock_repo_client):
    mock_repo_client.list_repositories.side_effect = Exception("API error")

    result = await list_repositories()
    data = json.loads(result)

    assert data["error"] == "Failed to list repositories."
