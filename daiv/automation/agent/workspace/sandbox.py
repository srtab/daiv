from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.git_manager import GitManager
from automation.agent.git_runners import SandboxGitRunner
from automation.agent.middlewares.file_system import READ_ONLY_PERMISSIONS, DAIVCompositeBackend
from automation.agent.workspace.sandbox_backend import SandboxFileBackend

if TYPE_CHECKING:
    from deepagents.backends.protocol import FileDownloadResponse

    from automation.agent.workspace.session import SandboxSession

logger = logging.getLogger("daiv.tools")


class SandboxWorkspace:
    """A sandbox run's workspace: the file tools, the ``bash`` tool and git all reach the container ``session`` holds,
    through one ``SandboxFileBackend``.

    The seed provisions the global skills. The file tools are unfenced, since bash reaches the whole container anyway;
    the explore subagent is only made read-only.
    """

    fs_permissions = None
    explore_permissions = READ_ONLY_PERMISSIONS
    provisions_skills = True

    def __init__(self, session: SandboxSession) -> None:
        self.session = session
        self._files = SandboxFileBackend(session)
        # A composite only so the offloading middlewares get an ``artifacts_root`` under /workspace: a bare backend
        # defaults to "/", and the sandbox rejects evictions written there.
        self.backend = DAIVCompositeBackend(default=self._files, routes={}, artifacts_root=WORKSPACE_PATH)
        self.git = GitManager(SandboxGitRunner(self._files))

    @property
    def bash(self) -> SandboxFileBackend:
        return self._files

    @property
    def is_ready(self) -> bool:
        return self.session.is_acquired

    async def authenticated_git(self) -> GitManager:
        """Re-mint the git-platform token onto the container, then hand back ``git`` (B8).

        Refreshing before the publish's first network command, rather than after a failed one, keeps this independent of
        git's auth-error wording. Best-effort: the mint and the proxy update raise a spread of platform and transport
        errors, and any of them only means the publish goes on with the turn-start token, so it is logged, not raised.
        """
        try:
            if await self.session.refresh_credential():
                logger.info("Refreshed the sandbox egress token of session %s before publish", self.session.session_id)
        except Exception:
            logger.exception(
                "Could not refresh the sandbox egress token of session %s before publish; proceeding with the "
                "turn-start token",
                self.session.session_id,
            )
        return self.git

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        (response,) = await self._files.adownload_files([path], max_bytes=max_bytes)
        return response
