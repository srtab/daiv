from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from codebase.utils import GitManager
from core.sandbox.client import DAIVSandboxClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from git import Repo


@asynccontextmanager
async def open_git_manager(*, session_id: str | None, gitrepo: Repo | None) -> AsyncIterator[GitManager]:
    """Yield a :class:`GitManager` matched to the run's mode.

    Sandbox-enabled runs (a ``session_id`` is present in state) get a **sandbox-mode**
    manager backed by a short-lived client against the live session — git runs in
    ``/workspace/repo`` where the agent's changes are authoritative. Sandbox-disabled /
    repoless runs (no session) get a **local-mode** manager over the GitPython clone,
    where the changes live on disk. The sandbox client is opened on entry and closed on
    exit; local mode owns no client.

    Args:
        session_id: The sandbox session id from agent state, or ``None`` when sandbox is disabled.
        gitrepo: The GitPython clone, used only in local mode.
    """
    if session_id:
        client = DAIVSandboxClient()
        await client.open()
        try:
            yield GitManager(client=client, session_id=session_id)
        finally:
            await client.close()
    else:
        yield GitManager(repo=gitrepo)
