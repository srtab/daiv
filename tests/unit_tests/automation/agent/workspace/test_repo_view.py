from unittest.mock import AsyncMock, MagicMock

from automation.agent.workspace.repo_view import RepoWorkspace


async def test_repo_workspace_delegates_all_but_git():
    base = MagicMock()
    base.download_file = AsyncMock(return_value="downloaded")
    git = MagicMock()
    authenticated = MagicMock()
    authenticate = AsyncMock(return_value=authenticated)

    view = RepoWorkspace(base, repo_path="/workspace/repos/g/mod", git=git, authenticate=authenticate)

    assert view.git is git
    assert view.repo_path == "/workspace/repos/g/mod"
    assert await view.authenticated_git() is authenticated
    for name in (
        "backend",
        "bash",
        "session",
        "fs_permissions",
        "provisions_skills",
        "skills_provisioning_errors",
        "is_ready",
    ):
        assert getattr(view, name) is getattr(base, name), name
    assert await view.download_file("/workspace/tmp/x", max_bytes=5) == "downloaded"
    base.download_file.assert_awaited_once_with("/workspace/tmp/x", max_bytes=5)
    handle = MagicMock()
    assert view.for_repo(handle, path="/workspace/repos/g/other") is base.for_repo.return_value
    base.for_repo.assert_called_once_with(handle, path="/workspace/repos/g/other")
