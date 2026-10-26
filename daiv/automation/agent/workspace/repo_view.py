from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse
    from deepagents.middleware.filesystem import FilesystemPermission

    from automation.agent.context import RepoHandle
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend
    from automation.agent.workspace.session import SandboxSession


class RepoWorkspace:
    """A run's workspace seen through a sibling repository: its ``git`` and ``authenticated_git`` reach that
    repository, and everything else is ``base``'s."""

    def __init__(
        self, base: Workspace, *, repo_path: str, git: GitManager, authenticate: Callable[[], Awaitable[GitManager]]
    ) -> None:
        self._base = base
        self.repo_path = repo_path
        self.git = git
        self._authenticate = authenticate

    @property
    def backend(self) -> BackendProtocol:
        return self._base.backend

    @property
    def bash(self) -> SandboxFileBackend | None:
        return self._base.bash

    @property
    def session(self) -> SandboxSession | None:
        return self._base.session

    @property
    def fs_permissions(self) -> list[FilesystemPermission] | None:
        return self._base.fs_permissions

    @property
    def provisions_skills(self) -> bool:
        return self._base.provisions_skills

    @property
    def skills_provisioning_errors(self) -> Sequence[str]:
        return self._base.skills_provisioning_errors

    @property
    def is_ready(self) -> bool:
        return self._base.is_ready

    async def authenticated_git(self) -> GitManager:
        return await self._authenticate()

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        return await self._base.download_file(path, max_bytes=max_bytes)

    def for_repo(self, handle: RepoHandle, *, path: str) -> Workspace:
        return self._base.for_repo(handle, path=path)
