from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from automation.agent.git_manager import GitManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from git import Repo

    from core.sandbox.client import DAIVSandboxClient


@asynccontextmanager
async def open_git_manager(
    *, client: DAIVSandboxClient | None = None, session_id: str | None, gitrepo: Repo | None
) -> AsyncIterator[GitManager]:
    """Yield a :class:`GitManager` matched to the run's mode.

    Sandbox-enabled runs (a ``session_id`` is present) get a **sandbox-mode** manager bound to the
    injected run-scoped ``client`` — git runs in ``/workspace/repo`` where the agent's changes are
    authoritative. The transport is borrowed, not owned: no open/close here, and no per-call
    fallback — a session id without a client is a wiring error. Sandbox-disabled / repoless runs
    (no session) get a **local-mode** manager over the GitPython clone.
    """
    if session_id:
        if client is None:
            raise RuntimeError("open_git_manager: sandbox session given but no sandbox client injected.")
        yield GitManager.for_sandbox(client, session_id)
    else:
        yield GitManager.for_local(cast("Repo", gitrepo))
