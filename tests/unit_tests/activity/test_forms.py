import uuid
from unittest import mock

import pytest
from activity.forms import AgentRunCreateForm
from activity.models import Activity, TriggerType
from activity.services import submit_ui_run
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
def test_submit_ui_run_enqueues_and_creates_activity(member_user):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)

    with mock.patch("activity.services.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        activity = submit_ui_run(user=member_user, prompt="do the thing", repo_id="acme/repo", ref="main", use_max=True)

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
def test_submit_ui_run_passes_none_for_empty_ref(member_user):
    task_id = uuid.uuid4()
    fake_task = _make_task_result(task_id)

    with mock.patch("activity.services.run_job_task") as m_task:
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        submit_ui_run(user=member_user, prompt="x", repo_id="acme/repo", ref="")

    assert m_task.aenqueue.await_args.kwargs["ref"] is None


def test_agent_run_form_requires_notify_on():
    """notify_on is required on the UI form — the view pre-fills it from the user's
    preference, so an empty submission is a client-side bug, not "defer"."""
    form = AgentRunCreateForm(data={"prompt": "do the thing", "repo_id": "x/y", "ref": "", "use_max": False})
    assert not form.is_valid()
    assert "notify_on" in form.errors


def test_agent_run_form_accepts_valid_notify_on():
    from notifications.choices import NotifyOn

    form = AgentRunCreateForm(
        data={"prompt": "p", "repo_id": "x/y", "ref": "", "use_max": False, "notify_on": NotifyOn.ON_FAILURE}
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["notify_on"] == NotifyOn.ON_FAILURE
