from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from automation.agent.constants import REPO_PATH
from automation.agent.workspace.backends import WORKSPACE_FENCE_PERMISSIONS, build_disk_workspace_backend
from automation.agent.workspace.git_manager import GitManager
from automation.agent.workspace.git_runners import LocalGitRunner
from automation.agent.workspace.repo_view import RepoWorkspace

if TYPE_CHECKING:
    from deepagents.backends.protocol import FileDownloadResponse

    from automation.agent.context import RepoHandle, RuntimeCtx
    from automation.agent.workspace.base import Workspace


class DiskWorkspace:
    """A disk-backed run's workspace: the worker's clone as ``/workspace/repo``, the shared skills cache as
    ``/workspace/skills`` and a per-run scratch dir for the rest, all reached through the file tools, since the run has
    no shell.

    The fence keeps those tools inside the three subtrees, and ``SkillsMiddleware`` copies the global skills into the
    cache. Git runs as a subprocess over the clone.
    """

    bash = None
    session = None
    fs_permissions = WORKSPACE_FENCE_PERMISSIONS
    provisions_skills = False
    skills_provisioning_errors = ()
    is_ready = True
    repo_path = REPO_PATH

    def __init__(self, ctx: RuntimeCtx) -> None:
        self._ctx = ctx
        self.backend = build_disk_workspace_backend(
            Path(ctx.gitrepo.working_dir),
            extra_clones={ctx.workspace_path(h): Path(h.gitrepo.working_dir) for h in ctx.extra_repos},
        )
        self.git = GitManager(LocalGitRunner(ctx.gitrepo))

    async def authenticated_git(self) -> GitManager:
        """A manager over the clone that overlays a credential env minted now: the clone's ``.git/config`` holds
        none."""
        auth_env = await sync_to_async(self._ctx.repo.client.get_git_auth_env)(self._ctx.repository)
        return GitManager(LocalGitRunner(self._ctx.gitrepo, auth_env=auth_env))

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        (response,) = await self.backend.adownload_files([path])
        return response

    def for_repo(self, handle: RepoHandle, *, path: str) -> Workspace:
        if path == REPO_PATH or handle is self._ctx.repo:
            return self

        async def authenticate() -> GitManager:
            auth_env = await sync_to_async(handle.client.get_git_auth_env)(handle.repository)
            return GitManager(LocalGitRunner(handle.gitrepo, auth_env=auth_env))

        return RepoWorkspace(
            self, repo_path=path, git=GitManager(LocalGitRunner(handle.gitrepo)), authenticate=authenticate
        )
