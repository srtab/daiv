import uuid
from unittest import mock

import pytest
from activity.models import TriggerType
from activity.services import RepoTarget, asubmit_batch_runs
from django_tasks_db.models import DBTaskResult, get_date_max
from sandbox_envs.models import SandboxEnvironment, Scope


async def _atask_result_row(task_id: uuid.UUID) -> mock.Mock:
    await DBTaskResult.objects.acreate(
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
@pytest.mark.asyncio
async def test_submit_batch_passes_sandbox_env_to_task_and_activity():
    from accounts.models import User

    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")

    fake_task = await _atask_result_row(uuid.uuid4())

    with (
        mock.patch("activity.services.run_job_task") as m_task,
        mock.patch("activity.services.generate_title_task") as m_title,
    ):
        m_task.aenqueue = mock.AsyncMock(return_value=fake_task)
        m_title.aenqueue = mock.AsyncMock()
        result = await asubmit_batch_runs(
            user=user,
            prompt="p",
            repos=[RepoTarget(repo_id="r/p")],
            trigger_type=TriggerType.UI_JOB,
            sandbox_environment_id=str(env.id),
        )

    assert m_task.aenqueue.await_args.kwargs["sandbox_environment_id"] == str(env.id)
    activity = result.activities[0]
    await activity.arefresh_from_db()
    assert activity.sandbox_environment_id == env.id
