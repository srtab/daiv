import threading
from unittest.mock import MagicMock

import pytest

from automation.agent.git_runners import LocalGitRunner
from automation.agent.middlewares.file_system import WORKSPACE_FENCE_PERMISSIONS
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
    assert workspace.provisions_skills is False


async def test_its_files_are_the_clone_under_workspace_repo(tmp_path):
    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "README.md").write_text("hello\n")

    [response] = await DiskWorkspace(_ctx(clone)).backend.adownload_files(["/workspace/repo/README.md"])

    assert response.content == b"hello\n"


def test_it_starts_from_its_own_backend(tmp_path):
    workspace = DiskWorkspace(_ctx(tmp_path))

    assert workspace.seed_backend is workspace.backend


def test_its_git_runs_over_the_clone_without_a_credential(tmp_path):
    ctx = _ctx(tmp_path)

    assert DiskWorkspace(ctx).git._runner == LocalGitRunner(ctx.gitrepo)


async def test_authenticated_git_mints_a_credential_each_time_it_is_asked(tmp_path, mock_repo_client):
    """A publish can come long after the executor built the workspace at turn start, so the credential is minted when
    the publish asks for it, and never before."""
    ctx = _ctx(tmp_path)
    auth_env = GitAuthEnv.for_token("https://gitlab.com/owner/repo.git", "tok")
    mint = mock_repo_client.get_git_auth_env
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


async def test_authenticated_git_mints_off_the_event_loop(tmp_path, mock_repo_client):
    """The mint is a blocking platform call, so it must not run on the loop the agent shares."""
    mint_threads = []

    def _mint(_repository):
        mint_threads.append(threading.get_ident())
        return GitAuthEnv.for_token("https://gitlab.com/owner/repo.git", "tok")

    mock_repo_client.get_git_auth_env.side_effect = _mint
    await DiskWorkspace(_ctx(tmp_path)).authenticated_git()

    assert mint_threads
    assert threading.get_ident() not in mint_threads


async def test_authenticated_git_leaves_the_credential_off_the_workspaces_own_git(tmp_path, mock_repo_client):
    ctx = _ctx(tmp_path)
    mock_repo_client.get_git_auth_env.return_value = GitAuthEnv.for_token("https://gitlab.com/owner/repo.git", "tok")

    workspace = DiskWorkspace(ctx)
    await workspace.authenticated_git()

    assert workspace.git._runner == LocalGitRunner(ctx.gitrepo)


async def test_a_failed_mint_is_raised(tmp_path, mock_repo_client):
    """No clone-side credential exists to fall back on, unlike a sandbox's turn-start token."""
    mock_repo_client.get_git_auth_env.side_effect = RuntimeError("mint failed")

    with pytest.raises(RuntimeError, match="mint failed"):
        await DiskWorkspace(_ctx(tmp_path)).authenticated_git()


async def test_a_file_in_the_scratch_dir_is_downloaded_through_the_backend(tmp_path):
    workspace = DiskWorkspace(_ctx(tmp_path))
    await workspace.backend.awrite("/workspace/tmp/report.md", "findings\n")

    response = await workspace.download_file("/workspace/tmp/report.md", max_bytes=1)

    assert (response.content, response.error) == (b"findings\n", None)


async def test_a_missing_file_downloads_as_an_error(tmp_path):
    response = await DiskWorkspace(_ctx(tmp_path)).download_file("/workspace/tmp/nope.md", max_bytes=1)

    assert response.content is None
    assert response.error is not None


async def test_a_directory_downloads_as_an_error(tmp_path):
    clone = tmp_path / "repo"
    (clone / "pkg").mkdir(parents=True)

    response = await DiskWorkspace(_ctx(clone)).download_file("/workspace/repo/pkg", max_bytes=1)

    assert response.content is None
    assert response.error is not None
