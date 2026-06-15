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
