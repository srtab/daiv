import uuid
from unittest.mock import patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max

from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig


@pytest.fixture
def platform_calls():
    """The GitLab/GitHub calls a webhook payload makes once it is processed."""
    with (
        patch.object(RepositoryConfig, "get_config") as get_config,
        patch.object(RepoClient, "create_instance") as create_instance,
    ):
        yield get_config, create_instance


async def make_db_task_result() -> uuid.UUID:
    task_id = uuid.uuid4()
    await DBTaskResult.objects.acreate(
        id=task_id,
        status="READY",
        task_path="webhooks.tasks.address_issue_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return task_id
