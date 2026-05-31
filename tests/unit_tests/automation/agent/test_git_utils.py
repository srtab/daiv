from unittest.mock import Mock

from automation.agent.git_utils import open_git_manager


async def test_open_git_manager_sandbox_mode_when_session_present(monkeypatch):
    """A session_id in state → sandbox-mode GitManager backed by a short-lived client
    that is opened on entry and closed on exit."""
    events = {"opened": False, "closed": False}

    class FakeClient:
        async def open(self):
            events["opened"] = True

        async def close(self):
            events["closed"] = True

    monkeypatch.setattr("automation.agent.git_utils.DAIVSandboxClient", FakeClient)

    async with open_git_manager(session_id="sid-123", gitrepo=None) as gm:
        assert gm._session_id == "sid-123"
        assert isinstance(gm._client, FakeClient)
        assert gm.repo is None
        assert events["opened"] is True

    assert events["closed"] is True


async def test_open_git_manager_local_mode_when_no_session():
    """No session_id → local-mode GitManager over the GitPython clone, no client opened."""
    repo = Mock()

    async with open_git_manager(session_id=None, gitrepo=repo) as gm:
        assert gm.repo is repo
        assert gm._client is None
