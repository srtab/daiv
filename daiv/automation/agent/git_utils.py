from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

from automation.agent.git_manager import GitManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from git import Repo

    from automation.agent.middlewares.file_system import SandboxFileBackend
    from codebase.clients.base import GitAuthEnv


@asynccontextmanager
async def open_git_manager(
    *,
    sandbox_backend: SandboxFileBackend | None,
    gitrepo: Repo | None,
    auth_env: GitAuthEnv | None = None,
    on_auth_failure: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[GitManager]:
    """Yield a :class:`GitManager` matched to the run's mode.

    Sandbox-enabled runs pass the run's bound :class:`SandboxFileBackend` — git runs in
    ``/workspace/repo`` where the agent's changes are authoritative. Sandbox-disabled /
    repoless runs pass ``sandbox_backend=None`` and get a local-mode manager over the
    GitPython clone.

    ``auth_env`` (``RepoClient.get_git_auth_env``) is overlaid on local-mode git subprocesses so
    network operations can authenticate — the clone's ``.git/config`` deliberately holds no
    credential. Only callers doing network git (push/fetch/ls-remote, i.e. the publisher) need it;
    offline callers (status/diff) can omit it. Sandbox mode ignores it: in-sandbox git
    authenticates via the egress proxy's injected header.

    ``on_auth_failure`` (the publisher's egress-token refresh) is wired onto **sandbox-mode** managers
    only: it re-mints the platform token on the live proxy when a remote git op (push/fetch/ls-remote/pull)
    is rejected because the turn outlived the token minted at turn start. Local mode ignores it — its
    credential is a fresh per-invocation ``auth_env`` overlay, so there is no live proxy to refresh.
    """
    if sandbox_backend is not None:
        yield GitManager.for_sandbox(sandbox_backend, on_auth_failure=on_auth_failure)
    else:
        yield GitManager.for_local(cast("Repo", gitrepo), auth_env=auth_env)
