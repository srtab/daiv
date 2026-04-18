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
def test_create_activity_persists_use_max_true(task_result_id):
    activity = create_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo", use_max=True
    )
    assert Activity.objects.get(pk=activity.pk).use_max is True


@pytest.mark.django_db
def test_create_activity_defaults_use_max_false(task_result_id):
    activity = create_activity(trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo")
    assert activity.use_max is False


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_acreate_activity_persists_use_max(task_result_id):
    activity = await acreate_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=task_result_id, repo_id="acme/repo", use_max=True
    )
    assert activity.use_max is True
