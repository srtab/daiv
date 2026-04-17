import uuid
from unittest import mock

import pytest
from activity.forms import AgentRunCreateForm
from activity.models import Activity, TriggerType
from django_tasks_db.models import DBTaskResult, get_date_max


def _make_task_result(task_id: uuid.UUID) -> mock.Mock:
    DBTaskResult.objects.create(
        id=task_id,
        status="READY",
        task_path="jobs.tasks.run_job_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return mock.Mock(id=task_id)


@pytest.mark.django_db(transaction=True)
def test_submit_enqueues_and_creates_activity(member_user):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    form = AgentRunCreateForm(data={"prompt": "do the thing", "repo_id": "acme/repo", "ref": "main", "use_max": True})
    assert form.is_valid(), form.errors

    with mock.patch("activity.forms.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        activity = form.submit(user=member_user)

    m_task.aenqueue.assert_awaited_once_with(repo_id="acme/repo", prompt="do the thing", ref="main", use_max=True)

    reloaded = Activity.objects.get(pk=activity.pk)
    assert reloaded.trigger_type == TriggerType.UI_JOB
    assert reloaded.use_max is True
    assert reloaded.repo_id == "acme/repo"
    assert reloaded.ref == "main"
    assert reloaded.prompt == "do the thing"
    assert reloaded.user == member_user
    assert reloaded.task_result_id == task_id


@pytest.mark.django_db(transaction=True)
def test_submit_passes_none_for_empty_ref(member_user):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)
    form = AgentRunCreateForm(data={"prompt": "x", "repo_id": "acme/repo", "ref": ""})
    assert form.is_valid(), form.errors

    with mock.patch("activity.forms.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        form.submit(user=member_user)

    kwargs = m_task.aenqueue.await_args.kwargs
    assert kwargs["ref"] is None
