from __future__ import annotations

import asyncio
import logging
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING

from git import GitCommandError

from automation.agent.constants import REPO_PATH

if TYPE_CHECKING:
    from git import Repo

    from automation.agent.middlewares.file_system import SandboxFileBackend

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


@dataclass(frozen=True)
class RepoStatus:
    """One-shot snapshot of the run's repo state, gathered in <=2 sandbox round-trips."""

    dirty: bool
    diff: str
    remote_branches: list[str]
    has_unpushed: bool


class GitManager:
    """Run git operations against a repository in one of two mutually-exclusive modes:

    - **Sandbox mode** (``sandbox_backend``): git runs *in the sandbox* via the bound
      backend's ``run_commands`` in ``repo_path`` (``/workspace/repo``). Used for
      sandbox-enabled runs, where the agent's changes are sandbox-authoritative (no local copy).
    - **Local mode** (``repo``): git runs as a subprocess against a GitPython clone's
      working tree. Used for sandbox-disabled / repoless runs, where changes live on disk.

    Exactly one mode must be configured. Every operation is async; local-mode git runs in
    a worker thread so the event loop is never blocked.

    Args:
        repo: GitPython repo for local mode.
        sandbox_backend: The run's bound :class:`SandboxFileBackend` for sandbox mode.
        repo_path: Repo path inside the sandbox (defaults to ``REPO_PATH``).
    """

    BRANCH_NAME_MAX_ATTEMPTS = 10

    def __init__(
        self,
        repo: Repo | None = None,
        *,
        sandbox_backend: SandboxFileBackend | None = None,
        repo_path: str | None = None,
    ) -> None:
        if (repo is None) == (sandbox_backend is None):
            raise ValueError("GitManager requires exactly one of `repo` (local) or `sandbox_backend` (sandbox).")
        if repo_path is None:
            repo_path = REPO_PATH
        self.repo = repo
        self._sandbox_backend = sandbox_backend
        self._repo_path = repo_path

    @classmethod
    def for_local(cls, repo: Repo) -> GitManager:
        """Local-mode manager over a GitPython clone (sandbox-disabled / repoless runs).

        Preferred over ``GitManager(repo)`` at call sites: it names the mode and makes the
        "exactly one mode" invariant unrepresentable by construction.
        """
        return cls(repo=repo)

    @classmethod
    def for_sandbox(cls, sandbox_backend: SandboxFileBackend, *, repo_path: str | None = None) -> GitManager:
        """Sandbox-mode manager that runs git in the session's ``repo_path`` (``/workspace/repo``).

        Takes the run's already-bound :class:`SandboxFileBackend` — the single session handle.
        The backend's ``_require_bound`` guard surfaces an unbound-session programming error on
        the first command, so no session id is threaded here.
        """
        return cls(sandbox_backend=sandbox_backend, repo_path=repo_path)

    # -- git invocation ------------------------------------------------------
    async def _git(self, *args: str, check: bool = True) -> _GitResult:
        """Run one git command in the repo. Raises ``GitCommandError`` on a non-zero
        exit when ``check`` is True; otherwise returns the result for the caller to inspect.
        """
        result = await (self._git_sandbox(args) if self._sandbox_backend is not None else self._git_local(args))
        if check and result.exit_code != 0:
            raise GitCommandError(["git", *args], result.exit_code, result.output)
        return result

    async def _git_sandbox(self, args: tuple[str, ...]) -> _GitResult:
        backend = self._sandbox_backend
        if backend is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in sandbox mode")
        command = " ".join(_shell_quote(token) for token in ("git", "-C", self._repo_path, *args))
        response = await backend.run_commands([command], fail_fast=True)
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

    async def _git_batch(self, commands: list[tuple[str, ...]]) -> list[_GitResult]:
        """Run several git commands in a single round-trip (sandbox) or concurrently (local).

        Never raises on a non-zero exit — callers inspect each result's exit_code, mirroring the
        per-command ``check=False`` discipline. ``fail_fast=False`` so an expected non-zero (e.g.
        ``diff --no-index`` finding differences) doesn't abort the batch. Results are in input order.
        """
        if not commands:
            return []
        if self._sandbox_backend is not None:
            cmd_strs = [
                " ".join(_shell_quote(tok) for tok in ("git", "-C", self._repo_path, *args)) for args in commands
            ]
            response = await self._sandbox_backend.run_commands(cmd_strs, fail_fast=False)
            if len(response.results) != len(commands):
                raise RuntimeError(f"Sandbox returned {len(response.results)} results for {len(commands)} git commands")
            return [_GitResult(exit_code=r.exit_code, output=r.output) for r in response.results]
        return list(await asyncio.gather(*(self._git_local(args) for args in commands)))

    @staticmethod
    def _append_untracked(diff: str, files: list[str], results: list[_GitResult]) -> str:
        """Fold per-untracked-file ``diff --no-index`` results into ``diff`` and normalise the trailing newline.

        ``diff --no-index`` exits 1 when it finds differences (expected — keep the output); exit >1 is a
        genuine error and must surface.
        """
        for fres, f in zip(results, files, strict=True):
            if fres.exit_code > 1:
                raise GitCommandError(["git", "diff", "--no-index", "/dev/null", f], fres.exit_code, fres.output)
            if fres.output:
                diff += f"\n{fres.output}"
        if diff and not diff.endswith("\n"):
            diff += "\n"
        return diff

    # -- queries -------------------------------------------------------------
    async def status_snapshot(self, *, base_branch: str, mr_source_branch: str | None) -> RepoStatus:
        """Collect everything the publisher needs in at most two sandbox round-trips.

        Batch A (one round-trip): working-tree status, diff vs ``origin/<base_branch>``, untracked
        file list, remote branch list, and — when ``mr_source_branch`` is given —
        ``origin/<mr_source_branch>..HEAD``. Batch B (only when untracked files exist): one
        ``diff --no-index`` per untracked file, in a single round-trip. Replaces the previous
        is_dirty/get_diff/has_unpushed/remote_branches sequence (~5+U round-trips) with <=2.
        """
        batch_a: list[tuple[str, ...]] = [
            ("status", "--porcelain"),
            ("diff", f"origin/{base_branch}"),
            ("ls-files", "--others", "--exclude-standard"),
            ("ls-remote", "--heads", "origin"),
        ]
        log_idx = -1
        if mr_source_branch:
            batch_a.append(("log", f"origin/{mr_source_branch}..HEAD", "--oneline"))
            log_idx = len(batch_a) - 1

        res = await self._git_batch(batch_a)
        status_res, diff_res, untracked_res, lsremote_res = res[0], res[1], res[2], res[3]

        # Exit-code discipline (same as the per-method versions): a real ref never exits non-zero for
        # "differences found", and an empty branch list from a failing ls-remote would risk a colliding
        # branch name, so raise rather than silently parse.
        if status_res.exit_code != 0:
            raise GitCommandError(["git", "status", "--porcelain"], status_res.exit_code, status_res.output)
        if diff_res.exit_code != 0:
            raise GitCommandError(["git", "diff", f"origin/{base_branch}"], diff_res.exit_code, diff_res.output)
        if untracked_res.exit_code != 0:
            raise GitCommandError(
                ["git", "ls-files", "--others", "--exclude-standard"], untracked_res.exit_code, untracked_res.output
            )
        if lsremote_res.exit_code != 0:
            raise GitCommandError(
                ["git", "ls-remote", "--heads", "origin"], lsremote_res.exit_code, lsremote_res.output
            )

        untracked = [line.strip() for line in untracked_res.output.splitlines() if line.strip()]
        batch_b = await self._git_batch([("diff", "--no-index", "/dev/null", f) for f in untracked])
        diff = self._append_untracked(diff_res.output, untracked, batch_b)

        if log_idx >= 0:
            log_res = res[log_idx]
            if log_res.exit_code != 0:
                logger.warning(
                    "status_snapshot: `git log origin/%s..HEAD` exited %s; treating as unpushed. Output: %s",
                    mr_source_branch,
                    log_res.exit_code,
                    log_res.output.strip(),
                )
                has_unpushed = True
            else:
                has_unpushed = bool(log_res.output.strip())
        else:
            has_unpushed = False

        return RepoStatus(
            dirty=bool(status_res.output.strip()),
            diff=diff,
            remote_branches=self._parse_remote_branches(lsremote_res.output),
            has_unpushed=has_unpushed,
        )

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
        logger.warning("git push auth failure: %s", result.output)
        raise GitPushPermissionError(
            "Failed to push changes to the remote repository due to authentication or permission issues. "
            "The credential embedded in the workspace may be expired (a session resumed a day or more "
            "after it was created holds an expired clone token — a fresh session re-clones with a new "
            "one), or branch protection rules may not allow this credential to push to this branch."
        )
    if _is_push_network_error_text(result.output):
        logger.warning("git push network failure: %s", result.output)
        raise GitPushNetworkError(
            "Failed to push changes: the remote host is unreachable. Sandbox-authoritative auto-commit "
            "pushes from inside the sandbox, so the sandbox environment must run with network access "
            "(network_enabled=True)."
        )
    raise GitCommandError(["git", *push_args], result.exit_code, result.output)
