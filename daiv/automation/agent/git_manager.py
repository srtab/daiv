from __future__ import annotations

import asyncio
import logging
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING

from git import GitCommandError

from automation.agent.constants import REPO_PATH
from core.sandbox.schemas import RunCommandsRequest

if TYPE_CHECKING:
    from git import Repo

    from core.sandbox.client import DAIVSandboxClient

logger = logging.getLogger("daiv.tools")


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
class _GitResult:
    """Normalized result of one git invocation (sandbox or local)."""

    exit_code: int
    output: str


class GitManager:
    """Run git operations against a repository in one of two mutually-exclusive modes:

    - **Sandbox mode** (``client`` + ``session_id``): git runs *in the sandbox* via
      ``run_commands`` in ``repo_path`` (``/workspace/repo``). Used for sandbox-enabled
      runs, where the agent's changes are sandbox-authoritative (no local copy).
    - **Local mode** (``repo``): git runs as a subprocess against a GitPython clone's
      working tree. Used for sandbox-disabled / repoless runs, where changes live on disk.

    Exactly one mode must be configured. Every operation is async; local-mode git runs in
    a worker thread so the event loop is never blocked.

    Args:
        repo: GitPython repo for local mode.
        client: Sandbox client for sandbox mode.
        session_id: Sandbox session id (required with ``client``).
        repo_path: Repo path inside the sandbox (defaults to ``REPO_PATH``).
    """

    BRANCH_NAME_MAX_ATTEMPTS = 10

    def __init__(
        self,
        repo: Repo | None = None,
        *,
        client: DAIVSandboxClient | None = None,
        session_id: str | None = None,
        repo_path: str | None = None,
    ) -> None:
        if (repo is None) == (client is None):
            raise ValueError("GitManager requires exactly one of `repo` (local) or `client` (sandbox).")
        if client is not None and not session_id:
            raise ValueError("GitManager sandbox mode requires a non-empty session_id.")
        if repo_path is None:
            repo_path = REPO_PATH
        self.repo = repo
        self._client = client
        self._session_id = session_id
        self._repo_path = repo_path

    @classmethod
    def for_local(cls, repo: Repo) -> GitManager:
        """Local-mode manager over a GitPython clone (sandbox-disabled / repoless runs).

        Preferred over ``GitManager(repo)`` at call sites: it names the mode and makes the
        "exactly one mode" invariant unrepresentable by construction.
        """
        return cls(repo=repo)

    @classmethod
    def for_sandbox(cls, client: DAIVSandboxClient, session_id: str, *, repo_path: str | None = None) -> GitManager:
        """Sandbox-mode manager that runs git in the session's ``repo_path`` (``/workspace/repo``).

        Preferred over ``GitManager(client=..., session_id=...)``: the required ``session_id`` is
        positional, so a sandbox manager can't be built without one.
        """
        return cls(client=client, session_id=session_id, repo_path=repo_path)

    # -- git invocation ------------------------------------------------------
    async def _git(self, *args: str, check: bool = True) -> _GitResult:
        """Run one git command in the repo. Raises ``GitCommandError`` on a non-zero
        exit when ``check`` is True; otherwise returns the result for the caller to inspect.
        """
        result = await (self._git_sandbox(args) if self._client is not None else self._git_local(args))
        if check and result.exit_code != 0:
            raise GitCommandError(["git", *args], result.exit_code, result.output)
        return result

    async def _git_sandbox(self, args: tuple[str, ...]) -> _GitResult:
        client, session_id = self._client, self._session_id
        if client is None or session_id is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in sandbox mode")
        command = " ".join(_shell_quote(token) for token in ("git", "-C", self._repo_path, *args))
        response = await client.run_commands(session_id, RunCommandsRequest(commands=[command], fail_fast=True))
        if not response.results:
            # The sandbox always returns one result per command; an empty list is a wire-level
            # anomaly. Fail with context rather than a bare IndexError on ``results[0]``.
            raise RuntimeError(f"Sandbox returned no result for: git {' '.join(args)}")
        result = response.results[0]
        return _GitResult(exit_code=result.exit_code, output=result.output)

    async def _git_local(self, args: tuple[str, ...]) -> _GitResult:
        repo = self.repo
        if repo is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in local mode")

        def _run() -> _GitResult:
            proc = subprocess.run(  # noqa: S603
                ["git", "-C", repo.working_dir, *args],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            return _GitResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr)

        return await asyncio.to_thread(_run)

    # -- queries -------------------------------------------------------------
    async def is_dirty(self) -> bool:
        """Whether the working tree has uncommitted changes (tracked or untracked)."""
        return bool((await self._git("status", "--porcelain")).output.strip())

    async def get_diff(self, ref: str = "HEAD") -> str:
        """Diff against ``ref``, including untracked files (via ``ls-files`` + ``diff --no-index``).

        A non-zero exit from ``git diff <ref>`` is a real error — plain ``git diff`` never uses
        exit 1 for "differences found" (only ``--no-index``/``--quiet`` do). So we raise rather than
        fold the ``fatal: ...`` text into the returned diff, which would otherwise corrupt
        commit-message / MR-metadata generation and the publish decision. The sole expected
        non-zero case is ``ref="HEAD"`` in a repo with no commits yet ("bad revision HEAD"), where
        we fall back to the staged diff (matching the prior behaviour for repoless/empty repos).
        """
        result = await self._git("diff", ref, check=False)
        if result.exit_code == 0:
            diff = result.output
        elif ref == "HEAD":
            diff = (await self._git("diff", "--cached", "--no-prefix", check=False)).output
        else:
            raise GitCommandError(["git", "diff", ref], result.exit_code, result.output)

        untracked = (await self._git("ls-files", "--others", "--exclude-standard")).output
        for file in (line.strip() for line in untracked.splitlines() if line.strip()):
            # `git diff --no-index` exits 1 when it finds differences (expected, keep the output);
            # exit >1 is a genuine error and must surface rather than be swallowed.
            file_result = await self._git("diff", "--no-index", "/dev/null", file, check=False)
            if file_result.exit_code > 1:
                raise GitCommandError(
                    ["git", "diff", "--no-index", "/dev/null", file], file_result.exit_code, file_result.output
                )
            if file_result.output:
                diff += f"\n{file_result.output}"
        if diff and not diff.endswith("\n"):
            diff += "\n"
        return diff

    async def has_unpushed(self, branch: str) -> bool:
        """Whether local HEAD has commits not present on ``origin/<branch>``.

        ``git log origin/<branch>..HEAD`` runs with ``check=False`` because a missing
        ``origin/<branch>`` ref (a branch never pushed yet, or not fetched) exits non-zero with
        ``fatal: ambiguous argument``. We branch on the exit code rather than the truthiness of the
        merged output, so a diagnostic line can't masquerade as commit output: a non-zero exit means
        the upstream doesn't resolve, i.e. nothing is pushed yet, so all of HEAD counts as unpushed.
        The failure is logged so a genuine git error is still visible.
        """
        result = await self._git("log", f"origin/{branch}..HEAD", "--oneline", check=False)
        if result.exit_code != 0:
            logger.warning(
                "has_unpushed: `git log origin/%s..HEAD` exited %s; treating as unpushed. Output: %s",
                branch,
                result.exit_code,
                result.output.strip(),
            )
            return True
        return bool(result.output.strip())

    # -- mutations -----------------------------------------------------------
    async def commit_all(self, message: str) -> None:
        """Stage every change and commit it. Callers should ensure the tree is dirty first
        (``git commit`` exits non-zero on an empty index)."""
        await self._git("add", "-A")
        await self._git("commit", "-m", message)

    async def push_head_to(self, branch: str, *, force: bool = False) -> str:
        """Push the current ``HEAD`` to ``origin/<branch>`` (creating it if needed).

        Raises ``GitPushPermissionError`` on an auth/permission failure,
        ``GitPushNetworkError`` when the remote host is unreachable (e.g. a network-disabled
        sandbox), and ``GitCommandError`` on any other push failure. Returns ``branch``.
        """
        push_args = ["push", "origin", f"HEAD:{branch}", *(["--force"] if force else [])]
        push = await self._git(*push_args, check=False)
        if push.exit_code != 0:
            _raise_for_push_failure(push_args, push)
        return branch

    async def remote_branches(self) -> list[str]:
        """Branch names that currently exist on ``origin``.

        ``ls-remote`` runs with ``check=True`` so an unreachable or erroring remote raises
        ``GitCommandError`` rather than silently parsing to ``[]`` — an empty list here would make
        ``unique_branch_name`` believe no branches exist and risk picking a colliding name.
        """
        return self._parse_remote_branches((await self._git("ls-remote", "--heads", "origin")).output)

    def unique_branch_name(self, original_branch_name: str, existing_branch_names: list[str]) -> str:
        """Public wrapper over :meth:`_gen_unique_branch_name` for callers outside this class."""
        return self._gen_unique_branch_name(original_branch_name, existing_branch_names)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _parse_remote_branches(output: str) -> list[str]:
        """Branch names from ``git ls-remote --heads origin`` (lines: ``<sha>\\trefs/heads/<branch>``)."""
        branches: list[str] = []
        for line in output.splitlines():
            ref = line.strip().split("\t")[-1]
            if ref.startswith("refs/heads/"):
                branches.append(ref[len("refs/heads/") :])
        return branches

    def _gen_unique_branch_name(
        self, original_branch_name: str, existing_branch_names: list[str], max_attempts: int = BRANCH_NAME_MAX_ATTEMPTS
    ) -> str:
        """
        Generate a unique branch name.

        Args:
            original_branch_name: The original branch name.
            existing_branch_names: The existing branch names.
            max_attempts: The maximum number of attempts to generate a unique branch name.

        Returns:
            A unique branch name.
        """
        suffix_count = 1
        branch_name = original_branch_name

        while branch_name in existing_branch_names and suffix_count < max_attempts:
            branch_name = f"{original_branch_name}-{suffix_count}"
            suffix_count += 1

        if suffix_count == max_attempts:
            raise ValueError(
                f"Failed to generate a unique branch name for {original_branch_name}, "
                f"max attempts reached {max_attempts}."
            )

        return branch_name


class GitPushPermissionError(RuntimeError):
    """
    Raised when pushing changes fails due to authentication or permission issues.
    """


class GitPushNetworkError(RuntimeError):
    """Raised when pushing fails because the remote host is unreachable.

    Typically a sandbox-authoritative run whose sandbox environment is configured with
    ``network_enabled=False``: git runs (and therefore pushes) from inside the sandbox, so the
    sandbox must have network access for the push to reach ``origin``.
    """


def _is_push_auth_error_text(output: str) -> bool:
    """
    Check if git push output indicates an authentication or permission failure.
    """
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "returned error: 403",
            "authentication failed",
            "permission denied",
            "access denied",
            "http basic: access denied",
            "could not read username",
            "not authorized",
        )
    )


def _is_push_network_error_text(output: str) -> bool:
    """Check if git push output indicates the remote host was unreachable (network failure).

    Kept distinct from the auth markers so a network-disabled sandbox produces an actionable
    ``GitPushNetworkError`` rather than a raw ``GitCommandError``. Checked only after the auth
    markers, so an auth failure that also mentions a URL is never misclassified as a network one.
    """
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "could not resolve host",
            "could not resolve proxy",
            "temporary failure in name resolution",
            "connection refused",
            "connection timed out",
            "failed to connect",
            "network is unreachable",
            "no route to host",
        )
    )


def _raise_for_push_failure(push_args: list[str], result: _GitResult) -> None:
    """Translate a failed ``git push`` into a typed, actionable error.

    Auth/permission → ``GitPushPermissionError``; an unreachable host → ``GitPushNetworkError``;
    anything else → the raw ``GitCommandError``. Auth is checked first so it always wins.
    """
    if _is_push_auth_error_text(result.output):
        raise GitPushPermissionError(
            "Failed to push changes to the remote repository due to authentication or permission issues."
        )
    if _is_push_network_error_text(result.output):
        raise GitPushNetworkError(
            "Failed to push changes: the remote host is unreachable. Sandbox-authoritative auto-commit "
            "pushes from inside the sandbox, so the sandbox environment must run with network access "
            "(network_enabled=True)."
        )
    raise GitCommandError(["git", *push_args], result.exit_code, result.output)
