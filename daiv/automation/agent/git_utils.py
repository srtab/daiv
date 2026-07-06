from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from automation.agent.git_manager import GitManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from git import Repo

    from automation.agent.middlewares.file_system import SandboxFileBackend


@asynccontextmanager
async def open_git_manager(
    *, sandbox_backend: SandboxFileBackend | None, gitrepo: Repo | None, local_auth_env: dict[str, str] | None = None
) -> AsyncIterator[GitManager]:
    """Yield a :class:`GitManager` matched to the run's mode.

    Sandbox-enabled runs pass the run's bound :class:`SandboxFileBackend` — git runs in
    ``/workspace/repo`` where the agent's changes are authoritative. Sandbox-disabled /
    repoless runs pass ``sandbox_backend=None`` and get a local-mode manager over the
    GitPython clone.

    ``local_auth_env`` (``RepoClient.get_git_auth_env``) is overlaid on local-mode git
    subprocesses so network operations can authenticate — the clone's ``.git/config``
    deliberately holds no credential. Only callers doing network git (push/fetch/ls-remote,
    i.e. the publisher) need it; offline callers (status/diff) can omit it. Sandbox mode
    ignores it: in-sandbox git authenticates via the egress proxy's injected header.
    """
    if sandbox_backend is not None:
        yield GitManager.for_sandbox(sandbox_backend)
    else:
        yield GitManager.for_local(cast("Repo", gitrepo), env=local_auth_env)
