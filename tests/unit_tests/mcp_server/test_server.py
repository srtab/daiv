import json
import uuid
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
async def test_get_job_status_not_found():
    result = await get_job_status(job_id=str(uuid.uuid4()))
    data = json.loads(result)
    assert data["error"] == "Job not found."


async def test_get_job_status_invalid_uuid():
    result = await get_job_status(job_id="not-a-uuid")
    data = json.loads(result)
    assert data["error"] == "Invalid job_id format."


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
