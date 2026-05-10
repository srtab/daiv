from unittest.mock import patch

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from codebase.base import Scope as RepoScope
from codebase.context import set_runtime_ctx


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
async def test_set_runtime_ctx_builds_sandbox_field(monkeypatch):
    """ctx.sandbox is populated from .daiv.yml + global default (no per-run override here)."""
    await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).adelete()
    await SandboxEnvironment.objects.acreate(
        scope=Scope.GLOBAL, name="Default", base_image="python:3.12", memory_bytes=2_000_000_000, is_default=True
    )
    monkeypatch.setattr("core.site_settings.site_settings.is_env_locked", lambda name: False)
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

            gc.return_value = RepositoryConfig.model_validate({"sandbox": {"base_image": "node:18"}})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL) as ctx:
                # .daiv.yml's explicit base_image beats the global default.
                assert ctx.sandbox.base_image == "node:18"
                # No memory_bytes in .daiv.yml → global default fills it in.
                assert ctx.sandbox.memory_bytes == 2_000_000_000
                # ctx.config.sandbox is still the raw .daiv.yml view.
                assert ctx.config.sandbox.base_image == "node:18"


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

            gc.return_value = RepositoryConfig.model_validate({"sandbox": {"base_image": "node:18"}})
            async with set_runtime_ctx(repo_id="r/p", scope=RepoScope.GLOBAL, sandbox_env_id=str(env.id)) as ctx:
                assert ctx.sandbox.base_image == "alpine:latest"
                assert ctx.sandbox.memory_bytes == 1_000_000_000
