from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.git_manager import GitManager
from automation.agent.git_runners import SandboxGitRunner
from automation.agent.middlewares.file_system import DAIVCompositeBackend, build_disk_workspace_backend
from automation.agent.workspace.sandbox_backend import SandboxFileBackend

if TYPE_CHECKING:
    from pathlib import Path

    from deepagents.backends.protocol import FileDownloadResponse

    from automation.agent.workspace.session import SandboxSession

logger = logging.getLogger("daiv.tools")


class SandboxWorkspace:
    """A sandbox run's workspace: the file tools, the ``bash`` tool and git all reach the container ``session`` holds,
    through one ``SandboxFileBackend``.

    The seed, from the worker's ``clone``, provisions the global skills. The file tools are unfenced, since bash reaches
    the whole container anyway.
    """

    fs_permissions = None
    provisions_skills = True

    def __init__(self, session: SandboxSession, *, clone: Path) -> None:
        self.session = session
        self.bash = SandboxFileBackend(session)
        # A composite only so the offloading middlewares get an ``artifacts_root`` under /workspace: a bare backend
        # defaults to "/", and the sandbox rejects evictions written there.
        self.backend = DAIVCompositeBackend(default=self.bash, routes={}, artifacts_root=WORKSPACE_PATH)
        self.seed_backend = build_disk_workspace_backend(clone)
        self.git = GitManager(SandboxGitRunner(self.bash))

    @property
    def is_ready(self) -> bool:
        return self.session.is_acquired

    async def authenticated_git(self) -> GitManager:
        """Re-mint the git-platform token onto the container, then hand back ``git``.

        Refreshing before the publish's first network command, rather than after a failed one, keeps this independent of
        git's auth-error wording. Best-effort: the mint and the proxy update raise a spread of platform and transport
        errors, and any of them only means the publish goes on with the turn-start token, so it is logged, not raised.
        An unacquired session is a caller bug, so that one is raised.
        """
        if not self.is_ready:
            raise RuntimeError("Cannot authenticate git before the sandbox session is acquired")
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
        (response,) = await self.bash.adownload_files([path], max_bytes=max_bytes)
        return response
