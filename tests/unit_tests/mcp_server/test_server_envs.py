from unittest.mock import AsyncMock, patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope


@pytest.mark.django_db(transaction=True)
async def test_list_environments_returns_user_and_global():
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="dev", base_image="x")
    await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="GlobalExtra", base_image="g")

    from mcp_server.server import list_environments

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        envs = await list_environments()
    names = sorted(env["name"] for env in envs)
    assert "dev" in names
    assert "GlobalExtra" in names


@pytest.mark.django_db(transaction=True)
async def test_submit_job_resolves_environment_name():
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="dev", base_image="alpine:latest")
    from mcp_server.server import submit_job

    fake_activity = type("A", (), {"task_result_id": "00000000-0000-0000-0000-000000000001"})()
    fake_result = type("R", (), {"batch_id": "b", "activities": [fake_activity], "failed": []})()
    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.asubmit_batch_runs", new=AsyncMock(return_value=fake_result)) as submit,
    ):
        await submit_job(prompt="p", repos=[{"repo_id": "r/p", "ref": ""}], environment="dev")
    assert submit.await_args.kwargs["sandbox_environment_id"] == str(env.id)


@pytest.mark.django_db(transaction=True)
async def test_submit_job_unknown_environment_returns_error():
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    from mcp_server.server import submit_job

    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.asubmit_batch_runs", new=AsyncMock()) as submit,
    ):
        result = await submit_job(prompt="p", repos=[{"repo_id": "r/p", "ref": ""}], environment="does-not-exist")
    import json

    data = json.loads(result)
    assert "error" in data
    assert "does-not-exist" in data["error"]
    submit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_get_environment_masks_secrets():
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[
            {"name": "PUB", "value": "shown", "is_secret": False},
            {"name": "SEC", "value": "hidden", "is_secret": True},
        ],
    )
    from mcp_server.server import get_environment

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        result = await get_environment("dev")
    assert result is not None
    by_name = {v["name"]: v["value"] for v in result["env_vars"]}
    assert by_name["PUB"] == "shown"
    assert by_name["SEC"] == "******"


@pytest.mark.django_db(transaction=True)
async def test_get_environment_unknown_returns_none():
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    from mcp_server.server import get_environment

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        result = await get_environment("missing")
    assert result is None
