import uuid

import pytest
from activity.models import Activity, TriggerType
from jobs.tasks import run_job_task

from codebase.exceptions import InvalidThreadResumeError

pytestmark = pytest.mark.django_db


async def test_run_job_task_rejects_repo_to_repoless_resume():
    thread_id = str(uuid.uuid4())
    await Activity.objects.acreate(
        trigger_type=TriggerType.MCP_JOB, repo_id="org/repo", thread_id=thread_id, prompt="prior"
    )
    with pytest.raises(InvalidThreadResumeError):
        await run_job_task.func(repo_id=None, prompt="x", thread_id=thread_id)


async def test_run_job_task_rejects_repoless_to_repo_resume():
    thread_id = str(uuid.uuid4())
    await Activity.objects.acreate(trigger_type=TriggerType.MCP_JOB, repo_id=None, thread_id=thread_id, prompt="prior")
    with pytest.raises(InvalidThreadResumeError):
        await run_job_task.func(repo_id="org/repo", prompt="x", thread_id=thread_id)


async def test_run_job_task_requires_thread_id():
    with pytest.raises(ValueError):
        await run_job_task.func(repo_id=None, prompt="x", thread_id="")
