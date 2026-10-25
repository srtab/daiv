from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

from automation.agent.workspace.backends import WORKSPACE_FENCE_PERMISSIONS, build_disk_workspace_backend
from automation.agent.workspace.git_manager import GitManager
from automation.agent.workspace.git_runners import LocalGitRunner

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import FileDownloadResponse
    from plugins.runtime import ResolvedPlugin

    from automation.agent.context import RuntimeCtx


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

    def __init__(self, ctx: RuntimeCtx) -> None:
        self._ctx = ctx
        self._clone_dir = Path(ctx.gitrepo.working_dir)
        self.backend = build_disk_workspace_backend(self._clone_dir)
        self.git = GitManager(LocalGitRunner(ctx.gitrepo))

    async def authenticated_git(self) -> GitManager:
        """A manager over the clone that overlays a credential env minted now: the clone's ``.git/config`` holds
        none."""
        auth_env = await sync_to_async(self._ctx.repo.client.get_git_auth_env)(self._ctx.repository)
        return GitManager(LocalGitRunner(self._ctx.gitrepo, auth_env=auth_env))

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        (response,) = await self.backend.adownload_files([path])
        return response

    async def provision_plugins(self, plugins: Sequence[ResolvedPlugin]) -> list[str]:
        self.backend = build_disk_workspace_backend(
            self._clone_dir, plugin_roots={plugin.name: plugin.host_root for plugin in plugins}
        )
        return []
