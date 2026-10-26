from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol

    from automation.agent.workspace.git_manager import GitManager


@dataclass(frozen=True)
class RepoWorkspace:
    """An extra repository's ``PublishWorkspace``: the run's ``backend``, and git in the repository at ``repo_path``,
    which ``authenticate`` mints a credential for."""

    backend: BackendProtocol
    repo_path: str
    authenticate: Callable[[], Awaitable[GitManager]]

    async def authenticated_git(self) -> GitManager:
        return await self.authenticate()
