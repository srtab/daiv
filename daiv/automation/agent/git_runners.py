from __future__ import annotations

import asyncio
import os
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from automation.agent.constants import REPO_PATH

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
    failures can catch this without also swallowing programming bugs (mode-mismatch
    guards, asyncio misuse), which must propagate.
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


@dataclass(frozen=True)
class LocalGitRunner:
    """Runs git as a subprocess over the worker's clone: a disk-backed run's repository.

    ``auth_env`` (``RepoClient.get_git_auth_env``) is overlaid on every subprocess's environment so network git
    (push/fetch/ls-remote) can authenticate — the clone's ``.git/config`` deliberately holds no credential. Offline
    callers (status/diff) leave it out. Each command runs in a worker thread, so the event loop never blocks.
    """

    repo: Repo
    auth_env: GitAuthEnv | None = None

    async def run(self, args: tuple[str, ...]) -> GitResult:
        # Disable every credential prompt path: with no credential in .git/config, an auth-required
        # remote otherwise makes git prompt (tty, or an inherited SSH_ASKPASS GUI helper) and hang an
        # unattended publish forever. GIT_TERMINAL_PROMPT=0 kills the tty prompt; empty GIT_ASKPASS
        # short-circuits the askpass fallback chain. Failing fast yields "could not read Username",
        # which is_git_auth_error_text classifies as an auth rejection. The credential overlay carries
        # the same prompt-disabling vars (via as_env), so the no-credential branch sets them itself.
        # Materialised here, at the innermost boundary, so the plaintext credential never lives as a
        # named local in an outer frame.
        overlay = self.auth_env.as_env() if self.auth_env else {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
        working_dir = self.repo.working_dir

        def _run() -> GitResult:
            proc = subprocess.run(  # noqa: S603
                ["git", "-C", working_dir, *args],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **overlay},
            )
            return GitResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr)

        return await asyncio.to_thread(_run)

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
            # The sandbox always returns one result per command; an empty list is a wire-level
            # anomaly. Fail with context rather than a bare IndexError on ``results[0]``.
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
