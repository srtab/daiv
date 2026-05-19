import pytest
from activity.models import Activity, TriggerType
from activity.services import acreate_activity
from sandbox_envs.models import SandboxEnvironment, Scope


@pytest.fixture
def user_factory(db):
    from accounts.models import User

    counter = {"n": 0}

    async def _make():
        counter["n"] += 1
        n = counter["n"]
        return await User.objects.acreate_user(username=f"u{n}", email=f"u{n}@e.com", password="x")  # noqa: S106

    return _make


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_activity_can_be_linked_to_sandbox_env(user_factory):
    user = await user_factory()
    env = await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    activity = await acreate_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=None, repo_id="r/p", user=user, sandbox_environment=env
    )
    await activity.arefresh_from_db()
    assert activity.sandbox_environment_id == env.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_activity_sandbox_env_set_null_on_env_delete(user_factory):
    user = await user_factory()
    env = await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    activity = await acreate_activity(
        trigger_type=TriggerType.UI_JOB, task_result_id=None, repo_id="r/p", user=user, sandbox_environment=env
    )
    await env.adelete()
    activity = await Activity.objects.aget(pk=activity.pk)
    assert activity.sandbox_environment_id is None
