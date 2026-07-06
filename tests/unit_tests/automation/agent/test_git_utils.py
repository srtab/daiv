from unittest.mock import MagicMock

from automation.agent.git_utils import open_git_manager
from codebase.clients.utils import GitAuthEnv


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
    """Local-mode git needs the per-invocation credential overlay (the clone persists none)."""
    repo = MagicMock()
    auth_env = GitAuthEnv.for_token("https://gitlab.com/g/r.git", "tok")
    async with open_git_manager(sandbox_backend=None, gitrepo=repo, auth_env=auth_env) as gm:
        assert gm._auth_env is auth_env


async def test_open_git_manager_sandbox_mode_ignores_auth_env():
    """Sandbox git authenticates via the egress proxy — no credential must leak into it."""
    backend = MagicMock()
    auth_env = GitAuthEnv.for_token("https://gitlab.com/g/r.git", "tok")
    async with open_git_manager(sandbox_backend=backend, gitrepo=None, auth_env=auth_env) as gm:
        assert gm._sandbox_backend is backend
        assert gm._auth_env is None
