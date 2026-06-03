from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from codebase.base import Scope as RepoScope
from codebase.context import set_runtime_ctx
from core.sandbox.client import _run_sandbox_client


@pytest.fixture
def user_factory(db):
    from accounts.models import User

    counter = {"n": 0}

    async def _make():
        counter["n"] += 1
        return await User.objects.acreate_user(
            username=f"u{counter['n']}",
            email=f"u{counter['n']}@e.com",
            password="x",  # noqa: S106
        )

    return _make


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_builds_sandbox_field():
    """ctx.sandbox is populated from the GLOBAL default env when no per-run env is supplied."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", memory_bytes=2_000_000_000, is_default=True
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.12"
                assert ctx.sandbox.memory_bytes == 2_000_000_000


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_honors_per_run_env(user_factory):
    user = await user_factory()
    env = await SandboxEnvironment.objects.acreate(
        scope=Scope.USER, user=user, name="dev", base_image="alpine:latest", memory_bytes=1_000_000_000
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL, sandbox_env_id=str(env.id)) as ctx:
                assert ctx.sandbox.base_image == "alpine:latest"
                assert ctx.sandbox.memory_bytes == 1_000_000_000


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_auto_resolves_repo_env():
    """When no explicit sandbox_env_id is supplied, the resolver picks up a
    GLOBAL env whose repo_ids matches the run's repo_id."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", is_default=True
    )
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="django-env", base_image="python:3.14", repo_ids=["acme/foo"]
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="acme/foo", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.14"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_set_runtime_ctx_auto_falls_back_to_global_default():
    """No repo_ids match → Auto returns the GLOBAL default."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", is_default=True
    )
    with patch("codebase.context.RepoClient.create_instance") as mock_client_factory:
        client = mock_client_factory.return_value
        client.get_repository.return_value = type("R", (), {"name": "x"})()
        client.git_platform = "gitlab"
        client.current_user.username = "bot"
        ctx_mgr = client.load_repo.return_value
        ctx_mgr.__enter__.return_value = type("Repo", (), {})()
        ctx_mgr.__exit__ = lambda *a: None

        with patch("codebase.context.RepositoryConfig.get_config") as gc:
            from codebase.repo_config import RepositoryConfig

            gc.return_value = RepositoryConfig.model_validate({})
            async with set_runtime_ctx(repo_id="acme/foo", scope=RepoScope.GLOBAL) as ctx:
                assert ctx.sandbox.base_image == "python:3.12"


def _patch_context_deps(*, sandbox_enabled: bool):
    repo_client = MagicMock()
    repo_client.get_repository.return_value = MagicMock()
    repo_client.current_user.username = "daiv"
    repo_client.load_repo.return_value = nullcontext(MagicMock(working_dir="/tmp/repo"))  # noqa: S108
    sandbox = MagicMock()
    sandbox.enabled = sandbox_enabled
    return (
        patch.multiple(
            "codebase.context",
            RepoClient=MagicMock(create_instance=MagicMock(return_value=repo_client)),
            RepositoryConfig=MagicMock(get_config=MagicMock(return_value=MagicMock(default_branch="main"))),
        ),
        patch("sandbox_envs.services.resolve_env_for_run", AsyncMock(return_value=None)),
        patch("sandbox_envs.services.get_global_default", AsyncMock(return_value=None)),
        patch("sandbox_envs.services.merge_sandbox_runtime", MagicMock(return_value=sandbox)),
        patch("sandbox_envs.services.row_to_override", MagicMock(return_value=None)),
    )


async def test_set_runtime_ctx_opens_and_closes_transport_when_sandbox_enabled():
    fake_client = MagicMock()
    fake_client.open = AsyncMock(return_value=fake_client)
    fake_client.close = AsyncMock()
    p = _patch_context_deps(sandbox_enabled=True)
    with p[0], p[1], p[2], p[3], p[4], patch("codebase.context.DAIVSandboxClient", return_value=fake_client):
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL):
            assert _run_sandbox_client.get() is fake_client
        fake_client.open.assert_awaited_once()
        fake_client.close.assert_awaited_once()
        assert _run_sandbox_client.get() is None


async def test_set_runtime_ctx_skips_transport_when_sandbox_disabled():
    p = _patch_context_deps(sandbox_enabled=False)
    with p[0], p[1], p[2], p[3], p[4], patch("codebase.context.DAIVSandboxClient") as ctor:
        async with set_runtime_ctx("repo-1", scope=RepoScope.GLOBAL):
            assert _run_sandbox_client.get() is None
        ctor.assert_not_called()
