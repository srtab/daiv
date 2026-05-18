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
    targets = submit.await_args.kwargs["repos"]
    assert [t.sandbox_environment_id for t in targets] == [str(env.id)]


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


@pytest.mark.django_db(transaction=True)
async def test_get_environment_swallows_lookup_error_and_logs():
    """``resolve_env_for_user`` raises ``LookupError`` for unknown names with a candidate
    list in the message. ``get_environment`` must swallow that to ``None`` (its contract)
    while still logging the message so the typo-vs-permissions distinction is observable."""
    import logging

    from accounts.models import User

    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    from mcp_server.server import get_environment

    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch(
            "mcp_server.server.resolve_env_for_user",
            new=AsyncMock(side_effect=LookupError("unknown environment 'typo'; valid: ['dev']")),
        ),
        patch.object(logging.getLogger("daiv.mcp_server"), "warning") as m_warn,
    ):
        result = await get_environment("typo")
    assert result is None
    m_warn.assert_called_once()
    assert "typo" in m_warn.call_args.args[1].args[0]


@pytest.mark.django_db(transaction=True)
async def test_list_environments_excludes_other_users_envs():
    """list_environments must not leak other users' USER envs."""
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    other = await User.objects.acreate_user(username="o", email="o@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=user, name="mine", base_image="x")
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=other, name="theirs", base_image="x")

    from mcp_server.server import list_environments

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        envs = await list_environments()
    names = {env["name"] for env in envs}
    assert "mine" in names
    assert "theirs" not in names


@pytest.mark.django_db(transaction=True)
async def test_get_environment_does_not_leak_other_users_env():
    """get_environment must return None when the named env belongs to another user."""
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    other = await User.objects.acreate_user(username="o", email="o@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=other, name="theirs", base_image="x")

    from mcp_server.server import get_environment

    with patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)):
        result = await get_environment("theirs")
    assert result is None


@pytest.mark.django_db(transaction=True)
async def test_submit_job_cannot_resolve_other_users_env():
    """submit_job's resolve_env_for_user must refuse another user's env name."""
    from accounts.models import User

    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    user = await User.objects.acreate_user(username="u", email="u@e.com", password="x")  # noqa: S106
    other = await User.objects.acreate_user(username="o", email="o@e.com", password="x")  # noqa: S106
    await SandboxEnvironment.objects.acreate(scope=Scope.USER, user=other, name="theirs", base_image="x")
    from mcp_server.server import submit_job

    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(return_value=user)),
        patch("mcp_server.server.asubmit_batch_runs", new=AsyncMock()) as submit,
    ):
        result = await submit_job(prompt="p", repos=[{"repo_id": "r/p", "ref": ""}], environment="theirs")
    import json

    data = json.loads(result)
    assert "error" in data
    assert "theirs" in data["error"]
    submit.assert_not_awaited()


@pytest.mark.django_db(transaction=True)
async def test_submit_job_auth_failure_returns_error_without_running():
    """If get_current_user raises, submit_job must fail closed instead of
    proceeding anonymously (which would degrade scope to GLOBAL-only)."""
    from mcp_server.server import submit_job

    with (
        patch("mcp_server.server.get_current_user", new=AsyncMock(side_effect=RuntimeError("token check failed"))),
        patch("mcp_server.server.asubmit_batch_runs", new=AsyncMock()) as submit,
    ):
        result = await submit_job(prompt="p", repos=[{"repo_id": "r/p", "ref": ""}])
    import json

    data = json.loads(result)
    assert "error" in data
    assert "auth" in data["error"].lower()
    submit.assert_not_awaited()
