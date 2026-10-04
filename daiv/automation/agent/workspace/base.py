"""What a run's agent works in: its files, its git and, on a sandbox run, its shell.

The run executor builds one workspace per run, a ``disk.DiskWorkspace`` over the worker's clone or a
``sandbox.SandboxWorkspace`` over the run's sandbox session, and ``create_daiv_agent`` hands it to every part of the
agent whose behaviour depends on which one it is. Those parts ask the workspace, so none of them branches on the mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendProtocol, FileDownloadResponse
    from deepagents.middleware.filesystem import FilesystemPermission

    from automation.agent.git_manager import GitManager
    from automation.agent.workspace.sandbox_backend import SandboxFileBackend
    from automation.agent.workspace.session import SandboxSession


class Workspace(Protocol):
    @property
    def backend(self) -> BackendProtocol:
        """The ``/workspace`` namespace the file tools, the skills and the subagents read and write."""
        ...

    @property
    def seed_backend(self) -> BackendProtocol:
        """The ``/workspace`` namespace as the run starts, readable while the agent is built: ``backend`` on disk; on a
        sandbox, the worker's clone that seeds the container, since the session is acquired only once the run starts.
        A reused warm container can still hold edits a previous turn failed to publish, which the clone lacks."""
        ...

    @property
    def git(self) -> GitManager:
        """Git in the repository the agent changes, for commands that need no platform credential."""
        ...

    @property
    def bash(self) -> SandboxFileBackend | None:
        """Runs the ``bash`` tool's commands; ``None`` when the run has no shell, so the agent gets no ``bash`` tool."""
        ...

    @property
    def session(self) -> SandboxSession | None:
        """The sandbox session ``SandboxMiddleware`` acquires and the run executor releases; ``None`` on disk."""
        ...

    @property
    def fs_permissions(self) -> list[FilesystemPermission] | None:
        """The file tools' rules for the main agent and its subagents (the explore subagent adds read-only on top);
        ``None`` leaves them unfenced."""
        ...

    @property
    def provisions_skills(self) -> bool:
        """Whether the global skills are already under ``/workspace/skills``; if not, ``SkillsMiddleware`` copies
        them."""
        ...

    @property
    def is_ready(self) -> bool:
        """Whether the files and git can be reached yet: always on disk; on a sandbox, once its session is acquired,
        which a slash-command short-circuit or an early agent failure skips."""
        ...

    async def authenticated_git(self) -> GitManager:
        """``git``, with a git-platform credential minted now for a publish's network commands (ls-remote, fetch,
        push), so a turn that outlived its turn-start token still pushes."""
        ...

    async def download_file(self, path: str, *, max_bytes: int) -> FileDownloadResponse:
        """Read one ``/workspace`` file to copy it out of the run. A sandbox refuses a file over ``max_bytes`` with
        ``DOWNLOAD_TOO_LARGE`` before it crosses the wire; a disk read is local and returns the file whole, leaving the
        limit to the caller."""
        ...
