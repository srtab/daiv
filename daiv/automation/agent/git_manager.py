from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess  # noqa: S404
from dataclasses import dataclass
from typing import TYPE_CHECKING

from git import GitCommandError

from automation.agent.constants import REPO_PATH
from core.utils import is_git_auth_error_text

if TYPE_CHECKING:
    from typing import NoReturn

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


class SandboxGitProtocolError(RuntimeError):
    """The sandbox returned a malformed/missing result for a git command (wire-level anomaly).

    Distinct from a bare ``RuntimeError`` so callers that degrade git faults to soft
    failures can catch this without also swallowing programming bugs (mode-mismatch
    guards, asyncio misuse), which must propagate.
    """


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
        env: dict[str, str] | None = None,
    ) -> None:
        if (repo is None) == (sandbox_backend is None):
            raise ValueError("GitManager requires exactly one of `repo` (local) or `sandbox_backend` (sandbox).")
        if repo_path is None:
            repo_path = REPO_PATH
        self.repo = repo
        self._sandbox_backend = sandbox_backend
        self._repo_path = repo_path
        self._env = env

    @classmethod
    def for_local(cls, repo: Repo, *, env: dict[str, str] | None = None) -> GitManager:
        """Local-mode manager over a GitPython clone (sandbox-disabled / repoless runs).

        Preferred over ``GitManager(repo)`` at call sites: it names the mode and makes the
        "exactly one mode" invariant unrepresentable by construction.

        ``env`` is overlaid on every git subprocess's environment. Network operations
        (push/fetch/ls-remote) require it to carry the credential env from
        ``RepoClient.get_git_auth_env`` — the clone's ``.git/config`` deliberately holds no
        credential (it is seeded verbatim into the sandbox).
        """
        return cls(repo=repo, env=env)

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
            raise SandboxGitProtocolError(f"Sandbox returned no result for: git {' '.join(args)}")
        result = response.results[0]
        return _GitResult(exit_code=result.exit_code, output=result.output)

    async def _git_local(self, args: tuple[str, ...]) -> _GitResult:
        repo = self.repo
        if repo is None:  # pragma: no cover - guaranteed by __init__
            raise RuntimeError("GitManager is not in local mode")

        def _run() -> _GitResult:
            # Disable every credential prompt path: with no credential in .git/config, an
            # auth-required remote otherwise makes git prompt (tty, or an inherited SSH_ASKPASS
            # GUI helper) and hang an unattended publish forever. GIT_TERMINAL_PROMPT=0 kills the
            # tty prompt; empty GIT_ASKPASS short-circuits the askpass fallback chain. Failing
            # fast yields "could not read Username", which is_git_auth_error_text classifies as an
            # auth rejection. self._env (the credential env) overlays last so it can override.
            proc = subprocess.run(  # noqa: S603
                ["git", "-C", repo.working_dir, *args],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", **(self._env or {})},
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
                raise SandboxGitProtocolError(
                    f"Sandbox returned {len(response.results)} results for {len(commands)} git commands"
                )
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

    @staticmethod
    def _require_ok(args: tuple[str, ...], res: _GitResult) -> _GitResult:
        """Raise ``GitCommandError`` if a batched git command exited non-zero; else return it.

        Query methods run their commands with ``check=False`` (via ``_git_batch``) and gate each
        result through here so a real ref never silently parses a failure as "no output".
        """
        if res.exit_code != 0:
            raise GitCommandError(["git", *args], res.exit_code, res.output)
        return res

    @staticmethod
    def _nonempty_lines(output: str) -> list[str]:
        """Stripped, non-blank lines of git output (untracked/changed file lists)."""
        return [line.strip() for line in output.splitlines() if line.strip()]

    # -- queries -------------------------------------------------------------
    async def get_diff(self, ref: str = "HEAD") -> str:
        """Unified diff of the working tree vs ``ref``, including untracked files.

        Untracked files are folded in via per-file ``diff --no-index`` (a second
        round-trip, only when any exist). Unlike :meth:`status_snapshot` this never
        touches the remote, so it works on detached/offline clones (eval harnesses).
        ``ref`` must resolve — there is no empty-repo (``--cached``) fallback like the
        pre-sandbox implementation had.
        """
        specs: list[tuple[str, ...]] = [("diff", ref), ("ls-files", "--others", "--exclude-standard")]
        diff_res, untracked_res = await self._git_batch(specs)
        self._require_ok(specs[0], diff_res)
        self._require_ok(specs[1], untracked_res)

        untracked = self._nonempty_lines(untracked_res.output)
        batch_b = await self._git_batch([("diff", "--no-index", "/dev/null", f) for f in untracked])
        return self._append_untracked(diff_res.output, untracked, batch_b)

    async def get_changed_files(self, ref: str = "HEAD") -> list[str]:
        """Paths changed in the working tree vs ``ref``, including untracked files.

        The same scope as :meth:`get_diff` (so the two stay symmetric for callers that
        diagnose one via the other), but names come straight from git — no diff-header
        parsing, so paths with spaces/quotes are exact. One batched round-trip.
        """
        specs: list[tuple[str, ...]] = [("diff", "--name-only", ref), ("ls-files", "--others", "--exclude-standard")]
        changed_res, untracked_res = await self._git_batch(specs)
        self._require_ok(specs[0], changed_res)
        self._require_ok(specs[1], untracked_res)
        return self._nonempty_lines(changed_res.output) + self._nonempty_lines(untracked_res.output)

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
        self._require_ok(batch_a[0], status_res)
        self._require_ok(batch_a[1], diff_res)
        self._require_ok(batch_a[2], untracked_res)
        self._require_ok(batch_a[3], lsremote_res)

        untracked = self._nonempty_lines(untracked_res.output)
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

    async def push_head_to(self, branch: str, *, force: bool = False, integrate_on_reject: bool = False) -> str:
        """Push the current ``HEAD`` to ``origin/<branch>`` (creating it if needed).

        When ``integrate_on_reject`` is set and a *non-fast-forward* rejection comes back — the
        remote branch advanced under the run, e.g. a dependabot force-push of its rebased PR
        branch, or a concurrent human push to an MR's source branch — the manager fetches the
        remote tip, rebases ``HEAD`` onto it, and retries the push once. This preserves the agent's
        work instead of discarding it. Pass it only when adding onto a branch is the intent (an
        existing MR's source branch); a fresh-branch push leaves it off so we never graft the run's
        commits onto unrelated history that happens to occupy a colliding ref.

        Raises ``GitPushStaleError`` on a non-fast-forward rejection that cannot be (or was asked not
        to be) integrated — including a rebase conflict or a remote that advanced again before the
        retry. Raises ``GitPushPermissionError`` on an auth/permission failure, ``GitPushNetworkError``
        when the remote host is unreachable (e.g. a network-disabled sandbox), and ``GitCommandError``
        on any other push failure. Returns ``branch``.
        """
        push_args = ["push", "origin", f"HEAD:{branch}", *(["--force"] if force else [])]
        push = await self._git(*push_args, check=False)
        if push.exit_code == 0:
            return branch

        # A non-fast-forward rejection is the only failure that integrating remote work can fix; an
        # auth/network/other failure won't change after a fetch+rebase, so it falls straight through
        # to classification below. On a successful integrate, retry the push and return on success.
        if integrate_on_reject and not force and _is_push_stale_error_text(push.output):
            await self._rebase_onto_remote(branch)  # raises GitPushStaleError on a rebase conflict
            push = await self._git(*push_args, check=False)
            if push.exit_code == 0:
                return branch

        # Either the push failed and we couldn't/didn't integrate, or the retry was still rejected
        # (remote advanced again) — classify whichever failed push result we are holding.
        _raise_for_push_failure(push_args, push)

    async def _rebase_onto_remote(self, branch: str) -> None:
        """Fetch ``origin/<branch>`` and rebase ``HEAD`` onto it so a non-fast-forward push can retry.

        On success ``HEAD`` is the run's commits replayed on top of the latest remote tip. A failed
        fetch is classified by transport (auth → ``GitPushPermissionError``, unreachable host →
        ``GitPushNetworkError``, else ``GitCommandError``) — via the operation-neutral
        :func:`_raise_for_transport_failure` rather than the push classifier, so a fetch never raises
        a nonsensical non-fast-forward error — keeping the actionable typed error the direct push
        would have produced instead of degrading to a raw ``GitCommandError``. On a rebase conflict
        the rebase is aborted (restoring the pre-rebase ``HEAD`` rather than stranding the workspace
        mid-rebase) and a typed :class:`GitPushStaleError` is raised. A *failed* abort cannot restore
        ``HEAD``: it is logged at error level (never silently swallowed) and flagged in the raised
        message, since the workspace is then left mid-rebase and must not be re-published.
        """
        fetch_args = ["fetch", "origin", branch]
        fetch = await self._git(*fetch_args, check=False)
        if fetch.exit_code != 0:
            _raise_for_transport_failure(fetch_args, fetch)
            raise GitCommandError(["git", *fetch_args], fetch.exit_code, fetch.output)

        rebase = await self._git("rebase", "FETCH_HEAD", check=False)
        if rebase.exit_code != 0:
            abort = await self._git("rebase", "--abort", check=False)
            if abort.exit_code != 0:
                logger.error(
                    "git rebase --abort failed after a conflict on '%s' (exit %s); the workspace is left "
                    "mid-rebase. Output: %s",
                    branch,
                    abort.exit_code,
                    abort.output.strip(),
                )
                raise GitPushStaleError(
                    f"The remote branch '{branch}' moved while DAIV was working and its changes conflict "
                    "with DAIV's. The conflicted rebase could not be aborted, so the workspace is in an "
                    "inconsistent state. Re-trigger DAIV to retry from a fresh clone."
                )
            raise GitPushStaleError(
                f"The remote branch '{branch}' moved while DAIV was working and its changes conflict "
                "with DAIV's, so they could not be rebased automatically. Re-trigger DAIV to retry "
                "against the updated branch."
            )

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

    def unique_branch_name(
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


class GitPushStaleError(RuntimeError):
    """Raised when a push is rejected as non-fast-forward and cannot be integrated.

    The remote branch advanced under the run (a dependabot force-push of its rebased PR branch, or a
    concurrent push to an MR's source branch) so ``HEAD`` is no longer a descendant of the remote tip.
    Kept distinct from the raw ``GitCommandError`` so callers surface an actionable "the branch moved;
    re-trigger" note instead of crashing the task on an inherently transient race.
    """


class GitPushNetworkError(RuntimeError):
    """Raised when pushing fails because the remote host is unreachable.

    git runs (and therefore pushes) from inside the sandbox, so the sandbox must be able to reach
    ``origin``'s git platform. DAIV opens that host automatically whenever a platform token can be
    minted — even on a network-off env — so this typically means the egress proxy is unavailable, or
    the run is one that legitimately has no platform token (e.g. an eval/benchmark run) and so stays
    fully network-isolated.
    """


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


def _is_push_stale_error_text(output: str) -> bool:
    """Check if git push output indicates a non-fast-forward rejection (the remote branch advanced).

    Within :func:`_raise_for_push_failure` this is checked *after* the auth and network markers, so a
    stale rejection that also mentions a URL or host is never misclassified there, and a genuine
    auth/network failure never reads as stale. The early gate in :meth:`GitManager.push_head_to` also
    calls this before any auth/network check, which is safe because it only gates a recoverable
    fetch+rebase and real auth/network output does not contain these non-fast-forward markers.
    """
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "fetch first",
            "non-fast-forward",
            "updates were rejected because the remote contains work",
            "tip of your current branch is behind",
        )
    )


def _raise_for_transport_failure(args: list[str], result: _GitResult) -> None:
    """Raise a typed error for an auth/permission or unreachable-host git failure; return otherwise.

    Operation-neutral on purpose: a credential or host-reachability problem (and its remedy) is the
    same whether the failing command was a ``push`` or the ``fetch`` of a push-recovery rebase, so
    both share this classification. Returns (does not raise) when the output matches neither marker,
    leaving the caller to layer operation-specific classification (e.g. push's non-fast-forward
    branch) on top. Auth is checked before network so an auth failure that also names a host wins.
    """
    if is_git_auth_error_text(result.output):
        logger.warning("git transport auth failure: %s", result.output)
        raise GitPushPermissionError(
            "Failed to authenticate to the remote repository (authentication or permission issue). "
            "The short-lived credential used for this push may be expired (a session resumed a day or "
            "more after it was created holds an expired clone token — a fresh session re-clones with a "
            "new one), or branch protection rules may not allow this credential to write to this branch."
        )
    if _is_push_network_error_text(result.output):
        logger.warning("git transport network failure: %s", result.output)
        raise GitPushNetworkError(
            "Failed to reach the remote host (it is unreachable). DAIV runs git from inside the sandbox, "
            "so the sandbox environment must run as an egress-enabled sandbox."
        )


def _raise_for_push_failure(push_args: list[str], result: _GitResult) -> NoReturn:
    """Translate a failed ``git push`` into a typed, actionable error (always raises).

    Auth/permission → ``GitPushPermissionError``; an unreachable host → ``GitPushNetworkError`` (both
    via :func:`_raise_for_transport_failure`, checked first so they win); a non-fast-forward rejection
    → ``GitPushStaleError``; anything else → the raw ``GitCommandError``.
    """
    _raise_for_transport_failure(push_args, result)
    if _is_push_stale_error_text(result.output):
        logger.warning("git push non-fast-forward rejection: %s", result.output)
        raise GitPushStaleError(
            "Failed to push changes: the remote branch advanced while DAIV was working (a non-fast-forward "
            "rejection). DAIV could not integrate the remote changes automatically. Re-trigger DAIV to "
            "retry against the updated branch."
        )
    raise GitCommandError(["git", *push_args], result.exit_code, result.output)
