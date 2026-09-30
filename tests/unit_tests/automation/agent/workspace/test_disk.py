from unittest.mock import MagicMock, patch

from automation.agent.git_runners import LocalGitRunner
from automation.agent.middlewares.file_system import EXPLORE_DISK_PERMISSIONS, WORKSPACE_FENCE_PERMISSIONS
from automation.agent.workspace.disk import DiskWorkspace
from codebase.clients.base import GitAuthEnv


def _ctx(clone) -> MagicMock:
    ctx = MagicMock()
    ctx.gitrepo.working_dir = str(clone)
    return ctx


def test_a_disk_workspace_is_fenced_has_no_shell_and_is_always_ready(tmp_path):
    workspace = DiskWorkspace(_ctx(tmp_path))

    assert (workspace.bash, workspace.session, workspace.is_ready) == (None, None, True)
    assert workspace.fs_permissions is WORKSPACE_FENCE_PERMISSIONS
    assert workspace.explore_permissions is EXPLORE_DISK_PERMISSIONS
    assert workspace.provisions_skills is False


async def test_its_files_are_the_clone_under_workspace_repo(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "README.md").write_text("hello\n")

    [response] = await DiskWorkspace(_ctx(clone)).backend.adownload_files(["/workspace/repo/README.md"])

    assert response.content == b"hello\n"


def test_its_git_runs_over_the_clone_without_a_credential(tmp_path):
    ctx = _ctx(tmp_path)

    assert DiskWorkspace(ctx).git._runner == LocalGitRunner(ctx.gitrepo)


async def test_authenticated_git_mints_a_credential_each_time_it_is_asked(tmp_path):
    """A publish can come long after the executor built the workspace at turn start, so the credential is minted when
    the publish asks for it, and never before."""
    ctx = _ctx(tmp_path)
    auth_env = GitAuthEnv.for_token("https://gitlab.com/owner/repo.git", "tok")

    with patch("automation.agent.workspace.disk.RepoClient") as repo_client:
        mint = repo_client.create_instance.return_value.get_git_auth_env
        mint.return_value = auth_env
        workspace = DiskWorkspace(ctx)
        mint.assert_not_called()
        first = await workspace.authenticated_git()
        await workspace.authenticated_git()

    assert first._runner == LocalGitRunner(ctx.gitrepo, auth_env=auth_env)
    assert mint.call_count == 2
    mint.assert_called_with(ctx.repository)


async def test_a_disk_read_returns_the_file_whole_whatever_the_cap(tmp_path):
    """The cap bounds a transfer over the wire; a local read has none, so the artifact store enforces the limit."""
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "report.md").write_bytes(b"x" * 10)

    response = await DiskWorkspace(_ctx(clone)).download_file("/workspace/repo/report.md", max_bytes=1)

    assert (response.content, response.error) == (b"x" * 10, None)
