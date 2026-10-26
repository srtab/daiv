from __future__ import annotations

import logging
from time import monotonic
from typing import TYPE_CHECKING

from deepagents.backends.composite import CompositeBackend

from automation.agent.constants import REPO_PATH, WORKSPACE_PATH
from automation.agent.workspace.git_manager import GitManager
from automation.agent.workspace.git_runners import SandboxGitRunner
from automation.agent.workspace.repo_view import RepoWorkspace
from automation.agent.workspace.sandbox_backend import SandboxFileBackend

if TYPE_CHECKING:
    from deepagents.backends.protocol import FileDownloadResponse

    from automation.agent.context import RepoHandle
    from automation.agent.workspace.base import Workspace
    from automation.agent.workspace.session import SandboxSession

logger = logging.getLogger("daiv.tools")

# One refresh re-mints every repository's token, so the publishes of one turn end share it.
CREDENTIAL_REFRESH_WINDOW_SECONDS = 60


class SandboxWorkspace:
    """A sandbox run's workspace: the file tools, the ``bash`` tool and git all reach the container ``session`` holds,
    through one ``SandboxFileBackend``.

    The seed provisions the global skills. The file tools are unfenced, since bash reaches the whole container anyway.
    """

    fs_permissions = None
    provisions_skills = True
    repo_path = REPO_PATH

    def __init__(self, session: SandboxSession) -> None:
        self.session = session
        self._refreshed_at: float | None = None
        self.bash = SandboxFileBackend(session)
        # A composite only so the offloading middlewares get an ``artifacts_root`` under /workspace: a bare backend
        # defaults to "/", and the sandbox rejects evictions written there.
        self.backend = CompositeBackend(default=self.bash, routes={}, artifacts_root=WORKSPACE_PATH)
        self.git = GitManager(SandboxGitRunner(self.bash))

    @property
    def is_ready(self) -> bool:
        return self.session.is_acquired

    @property
    def skills_provisioning_errors(self) -> list[str]:
        return self.session.skills_seed_errors

    async def authenticated_git(self) -> GitManager:
        """Re-mint the git-platform tokens onto the container, unless a refresh in the last
        ``CREDENTIAL_REFRESH_WINDOW_SECONDS`` already did, then hand back ``git``.

        Refreshing before the publish's first network command, rather than after a failed one, keeps this independent of
        git's auth-error wording. Best-effort: the mint and the proxy update raise a spread of platform and transport
        errors, and any of them only means the publish goes on with the turn-start token, so it is logged, not raised.
        An unacquired session is a caller bug, so that one is raised.
        """
        await self._refresh_credential()
        return self.git

    async def _refresh_credential(self) -> None:
        if not self.is_ready:
            raise RuntimeError("Cannot authenticate git before the sandbox session is acquired")
        if self._refreshed_at is not None and monotonic() - self._refreshed_at < CREDENTIAL_REFRESH_WINDOW_SECONDS:
            return
        try:
            if await self.session.refresh_credential():
                logger.info("Refreshed the sandbox egress token of session %s before publish", self.session.session_id)
            self._refreshed_at = monotonic()
        except Exception:
            logger.exception(
                "Could not refresh the sandbox egress token of session %s before publish; proceeding with the "
                "turn-start token",
                self.session.session_id,
            )

    def for_repo(self, handle: RepoHandle, *, path: str) -> Workspace:
        if path == REPO_PATH:
            return self
        git = GitManager(SandboxGitRunner(self.bash, repo_path=path))

        async def authenticate() -> GitManager:
            await self._refresh_credential()
            return git

        return RepoWorkspace(self, repo_path=path, git=git, authenticate=authenticate)

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        (response,) = await self.bash.adownload_files([path], max_bytes=max_bytes)
        return response
