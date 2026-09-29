from __future__ import annotations

import asyncio
import os
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from git import GitCommandError

from automation.agent.constants import REPO_PATH
from codebase.clients.base import GIT_NO_PROMPT_ENV

if TYPE_CHECKING:
    from git import Repo

    from automation.agent.workspace.sandbox_backend import SandboxFileBackend
    from codebase.clients.base import GitAuthEnv


_SHELL_SAFE_ARG = re.compile(r"^[A-Za-z0-9_./@=:,+-]+$")


def _shell_quote(arg: str) -> str:
    """POSIX single-quote ``arg`` unless it is already shell-safe.

    Flags/refs/paths (``status``, ``--porcelain``, ``origin/main..HEAD``) pass through
    unquoted for readability; anything with spaces or shell metacharacters (commit
    messages, ``--format=%(...)``) is single-quoted so the sandbox shell sees it verbatim.
    """
    if arg and _SHELL_SAFE_ARG.match(arg):
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


@dataclass
class GitResult:
    """Normalized result of one git invocation (sandbox or local)."""

    exit_code: int
    output: str


class SandboxGitProtocolError(RuntimeError):
    """The sandbox returned a malformed/missing result for a git command (wire-level anomaly).

    Distinct from a bare ``RuntimeError`` so callers that degrade git faults to soft
    failures can catch this without also swallowing programming bugs (the unbound-session
    guard, asyncio misuse), which must propagate.
    """


class GitRunner(Protocol):
    """Where a :class:`~automation.agent.git_manager.GitManager` runs its git commands.

    Neither method raises on a non-zero exit: the manager decides which exits are failures.
    """

    async def run(self, args: tuple[str, ...]) -> GitResult:
        """Run ``git <args>`` in the repository."""
        ...

    async def run_batch(self, commands: list[tuple[str, ...]]) -> list[GitResult]:
        """Run every command, in one round-trip where the transport allows it, and return the results in input order.

        A non-zero exit never stops the rest: ``diff --no-index`` exits 1 when it finds differences.
        """
        ...


def _run_git(working_dir: str | os.PathLike[str], args: tuple[str, ...], auth_env: GitAuthEnv | None) -> GitResult:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", working_dir, *args],  # noqa: S607
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
            env={**os.environ, **GIT_NO_PROMPT_ENV, **(auth_env.as_env() if auth_env else {})},
        )
    except (OSError, ValueError) as exc:
        spawn_error = type(exc).__name__
    else:
        return GitResult(exit_code=proc.returncode, output=proc.stdout)
    # Raised outside the handler so no ``__context__`` keeps the frames holding the credential env alive.
    raise GitCommandError(["git", *args], -1, f"could not run git ({spawn_error})")


@dataclass(frozen=True)
class LocalGitRunner:
    """Runs git as a subprocess over the worker's clone: a disk-backed run's repository.

    ``auth_env`` (``RepoClient.get_git_auth_env``) is overlaid on every subprocess's environment so network git
    (push/fetch/ls-remote) can authenticate — the clone's ``.git/config`` deliberately holds no credential. Git's
    credential prompts are disabled with or without it, so an unauthenticated or rejected remote fails fast with
    ``could not read Username`` instead of hanging an unattended publish. ``output`` interleaves stderr with stdout,
    as the sandbox shell does. Each command runs in a worker thread, so the event loop never blocks.
    """

    repo: Repo
    auth_env: GitAuthEnv | None = None

    async def run(self, args: tuple[str, ...]) -> GitResult:
        return await asyncio.to_thread(_run_git, self.repo.working_dir, args, self.auth_env)

    async def run_batch(self, commands: list[tuple[str, ...]]) -> list[GitResult]:
        return list(await asyncio.gather(*(self.run(args) for args in commands)))


@dataclass(frozen=True)
class SandboxGitRunner:
    """Runs git in the sandbox's ``/workspace/repo``, where a sandbox run's changes live, through the run's backend.

    In-sandbox git authenticates through the egress proxy's injected header, so this runner carries no credential.
    """

    backend: SandboxFileBackend

    async def run(self, args: tuple[str, ...]) -> GitResult:
        response = await self.backend.run_commands([self._command(args)], fail_fast=True)
        if not response.results:
            raise SandboxGitProtocolError(f"Sandbox returned no result for: git {' '.join(args)}")
        result = response.results[0]
        return GitResult(exit_code=result.exit_code, output=result.output)

    async def run_batch(self, commands: list[tuple[str, ...]]) -> list[GitResult]:
        response = await self.backend.run_commands([self._command(args) for args in commands], fail_fast=False)
        if len(response.results) != len(commands):
            raise SandboxGitProtocolError(
                f"Sandbox returned {len(response.results)} results for {len(commands)} git commands"
            )
        return [GitResult(exit_code=r.exit_code, output=r.output) for r in response.results]

    @staticmethod
    def _command(args: tuple[str, ...]) -> str:
        return " ".join(_shell_quote(token) for token in ("git", "-C", REPO_PATH, *args))
