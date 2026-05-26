"""Activity persists the per-run agent override pair (agent_model + agent_thinking_level).

This file replaces the old ``use_max`` assertions: ``use_max`` is no longer written by
non-webhook surfaces (the column stays on the model for one release but is left at its
default ``False``).
"""

import uuid

import pytest
from activity.models import Activity, TriggerType
from activity.services import acreate_activity, create_activity
from django_tasks_db.models import DBTaskResult, get_date_max


@pytest.fixture
def task_result_id(db):
    result = DBTaskResult.objects.create(
        id=uuid.uuid4(),
        status="READY",
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return result.id


@pytest.mark.django_db
def test_create_activity_persists_override_pair(task_result_id):
    activity = create_activity(
        trigger_type=TriggerType.UI_JOB,
        task_result_id=task_result_id,
        repo_id="acme/repo",
        agent_model="openrouter:anthropic/claude-opus-4.6",
        agent_thinking_level="high",
    )
    stored = Activity.objects.get(pk=activity.pk)
    assert stored.agent_model == "openrouter:anthropic/claude-opus-4.6"
    assert stored.agent_thinking_level == "high"


@pytest.mark.django_db
def test_create_activity_defaults_override_pair_to_empty(task_result_id):
    activity = create_activity(trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo")
    assert activity.agent_model == ""
    assert activity.agent_thinking_level == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_acreate_activity_persists_override_pair(task_result_id):
    activity = await acreate_activity(
        trigger_type=TriggerType.UI_JOB,
        task_result_id=task_result_id,
        repo_id="acme/repo",
        agent_model="openrouter:anthropic/claude-opus-4.6",
        agent_thinking_level="high",
    )
    assert activity.agent_model == "openrouter:anthropic/claude-opus-4.6"
    assert activity.agent_thinking_level == "high"
