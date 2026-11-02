import uuid
from unittest.mock import patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.pipeline_watch.service import PipelineWatch

from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from tests.unit_tests.sessions.conftest import make_pipeline


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


class FailedHeadPipeline:
    """A platform whose merge request head pipeline failed on commit ``abc123``."""

    async def aensure_client(self):
        return None

    async def aread_pipeline(self, *, pipeline_id):
        return make_pipeline("failed", pipeline_id=pipeline_id)

    async def ais_head_pipeline(self, *, merge_request_iid, report):
        return True


@pytest.fixture
def evaluate_inline(monkeypatch):
    """Run the evaluation a CI webhook enqueues at once, against :class:`FailedHeadPipeline` and the repository's real
    watch policy, so a test sees the event it posts."""

    class InlineTask:
        async def aenqueue(self, *, repo_id, ref, pipeline_id):
            await PipelineWatch(repo_id, platform=FailedHeadPipeline()).aevaluate(ref=ref, pipeline_id=pipeline_id)

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", InlineTask())
    with patch("sessions.inbox.post.drain_logged"):
        yield
