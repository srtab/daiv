from unittest.mock import MagicMock

from automation.agent.git_utils import open_git_manager


async def test_open_git_manager_sandbox_uses_injected_backend():
    backend = MagicMock()
    async with open_git_manager(sandbox_backend=backend, gitrepo=None) as gm:
        assert gm._sandbox_backend is backend
        assert gm.repo is None


async def test_open_git_manager_local_mode_when_no_backend():
    repo = MagicMock()
    async with open_git_manager(sandbox_backend=None, gitrepo=repo) as gm:
        assert gm.repo is repo
        assert gm._sandbox_backend is None


async def test_open_git_manager_local_mode_forwards_auth_env():
    """Local-mode git needs the per-invocation credential env (the clone persists none)."""
    repo = MagicMock()
    env = {"GIT_CONFIG_COUNT": "1"}
    async with open_git_manager(sandbox_backend=None, gitrepo=repo, local_auth_env=env) as gm:
        assert gm._env == env


async def test_open_git_manager_sandbox_mode_ignores_auth_env():
    """Sandbox git authenticates via the egress proxy — no env credential must leak into it."""
    backend = MagicMock()
    async with open_git_manager(sandbox_backend=backend, gitrepo=None, local_auth_env={"X": "1"}) as gm:
        assert gm._sandbox_backend is backend
        assert gm._env is None
