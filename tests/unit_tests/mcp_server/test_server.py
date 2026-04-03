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
        result = await submit_job.fn(repo_id="group/project", prompt="Fix the bug")

    data = json.loads(result)
    assert "job_id" in data
    assert data["job_id"] == mock_result.id


@pytest.mark.django_db(transaction=True)
async def test_submit_job_failure():
    with patch("mcp_server.server.run_job_task") as mock_task:
        mock_task.aenqueue = AsyncMock(side_effect=Exception("DB down"))
        result = await submit_job.fn(repo_id="group/project", prompt="Fix the bug")

    data = json.loads(result)
    assert "error" in data


@pytest.mark.django_db(transaction=True)
async def test_get_job_status_not_found():
    result = await get_job_status.fn(job_id=str(uuid.uuid4()))
    data = json.loads(result)
    assert data["error"] == "Job not found."


async def test_get_job_status_invalid_uuid():
    result = await get_job_status.fn(job_id="not-a-uuid")
    data = json.loads(result)
    assert data["error"] == "Invalid job_id format."


def test_list_repositories_success(mock_repo_client):
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

    result = list_repositories.fn()
    data = json.loads(result)

    assert "repositories" in data
    assert len(data["repositories"]) == 1
    assert data["repositories"][0]["repo_id"] == "group/project"
    assert data["repositories"][0]["name"] == "project"
    assert data["repositories"][0]["default_branch"] == "main"
    assert data["repositories"][0]["topics"] == ["python"]


def test_list_repositories_empty(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    result = list_repositories.fn()
    data = json.loads(result)

    assert data["repositories"] == []


def test_list_repositories_with_search(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    list_repositories.fn(search="test")
    mock_repo_client.list_repositories.assert_called_with(search="test", topics=None)


def test_list_repositories_with_topics(mock_repo_client):
    mock_repo_client.list_repositories.return_value = []

    list_repositories.fn(topics=["python", "django"])
    mock_repo_client.list_repositories.assert_called_with(search=None, topics=["python", "django"])
