from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shlex
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple

from deepagents.backends.composite import CompositeBackend
from skills.constants import PLUGINS_PATH

from automation.agent.constants import WORKSPACE_PATH
from automation.agent.workspace.git_manager import GitManager
from automation.agent.workspace.git_runners import SandboxGitRunner
from automation.agent.workspace.sandbox_backend import SandboxFileBackend

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from deepagents.backends.protocol import FileDownloadResponse
    from plugins.runtime import ResolvedPlugin

    from automation.agent.workspace.session import SandboxSession
    from core.sandbox.schemas import RunCommandsResponse

logger = logging.getLogger("daiv.tools")

PLUGINS_FINGERPRINT_COMMAND = (
    f"find {PLUGINS_PATH} -type f -exec sha256sum {{}} + 2>/dev/null | LC_ALL=C sort | sha256sum"
)


class PluginFile(NamedTuple):
    host_path: Path
    target: str
    digest: str
    executable: bool


@lru_cache(maxsize=64)
def _plugin_files(host_root: Path, workspace_root: str) -> tuple[PluginFile, ...]:
    """The files of one plugin revision, which never changes on disk, so the listing is cached per worker."""
    files = []
    for host_path in sorted(host_root.rglob("*")):
        if host_path.is_symlink() or not host_path.is_file():
            continue
        target = f"{workspace_root}/{host_path.relative_to(host_root).as_posix()}"
        digest = hashlib.sha256(host_path.read_bytes()).hexdigest()
        files.append(PluginFile(host_path, target, digest, os.access(host_path, os.X_OK)))
    return tuple(files)


def _fingerprint(files: Iterable[PluginFile]) -> str:
    """What ``PLUGINS_FINGERPRINT_COMMAND`` prints for exactly ``files``: the hash of their sorted sha256sum lines."""
    lines = sorted(f"{file.digest}  {file.target}\n" for file in files)
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def _read_uploads(files: Sequence[PluginFile]) -> list[tuple[str, bytes]]:
    return [(file.target, file.host_path.read_bytes()) for file in files]


def _succeeded(response: RunCommandsResponse) -> bool:
    return bool(response.results) and all(result.exit_code == 0 for result in response.results)


class SandboxWorkspace:
    """A sandbox run's workspace: the file tools, the ``bash`` tool and git all reach the container ``session`` holds,
    through one ``SandboxFileBackend``.

    The seed provisions the global skills. The file tools are unfenced, since bash reaches the whole container anyway.
    """

    fs_permissions = None
    provisions_skills = True

    def __init__(self, session: SandboxSession) -> None:
        self.session = session
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

    async def provision_plugins(self, plugins: Sequence[ResolvedPlugin]) -> list[str]:
        """Replace ``/workspace/plugins`` with the files of ``plugins``, unless the files there already hash to exactly
        theirs: the sandbox outlives a turn, so a plugin file changed in an earlier turn is put back."""
        if not self.is_ready:
            return []
        listings = [
            await asyncio.to_thread(_plugin_files, plugin.host_root, plugin.workspace_root) for plugin in plugins
        ]
        if await self._plugins_fingerprint() == _fingerprint(file for listing in listings for file in listing):
            return []

        removal = await self.bash.run_commands([f"rm -rf {PLUGINS_PATH}"], fail_fast=True)
        if not _succeeded(removal):
            return ["The plugins could not be provisioned: the previous plugin files could not be removed"]
        errors: list[str] = []
        for listing in listings:
            if not listing:
                continue
            errors.extend(
                f"Plugin file {response.path} could not be provisioned: {response.error}"
                for response in await self.bash.aupload_files(await asyncio.to_thread(_read_uploads, listing))
                if response.error
            )
        executables = [file.target for listing in listings for file in listing if file.executable]
        if executables:
            chmod = await self.bash.run_commands(
                ["chmod +x " + " ".join(shlex.quote(path) for path in executables)], fail_fast=False
            )
            if not _succeeded(chmod):
                errors.append("The plugins could not be provisioned: their scripts could not be made executable")
        return errors

    async def _plugins_fingerprint(self) -> str | None:
        """The fingerprint of the files now under ``/workspace/plugins``, or None when it couldn't be taken."""
        response = await self.bash.run_commands([PLUGINS_FINGERPRINT_COMMAND], fail_fast=True)
        if len(response.results) != 1 or response.results[0].exit_code != 0:
            return None
        output = response.results[0].output.split(maxsplit=1)
        return output[0] if output else None
