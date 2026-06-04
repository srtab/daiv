from unittest.mock import MagicMock

import pytest

from automation.agent.git_utils import open_git_manager


async def test_open_git_manager_sandbox_uses_injected_client():
    client = MagicMock()
    async with open_git_manager(client=client, session_id="sess-1", gitrepo=None) as gm:
        assert gm._client is client
        assert gm._session_id == "sess-1"


async def test_open_git_manager_sandbox_requires_client():
    with pytest.raises(RuntimeError, match="no sandbox client"):
        async with open_git_manager(client=None, session_id="sess-1", gitrepo=None):
            pass


async def test_open_git_manager_local_mode_ignores_client():
    repo = MagicMock()
    async with open_git_manager(client=None, session_id=None, gitrepo=repo) as gm:
        assert gm.repo is repo
        assert gm._client is None
