from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from git import GitCommandError

from core.utils import is_git_auth_error_text

if TYPE_CHECKING:
    from typing import NoReturn

    from automation.agent.git_runners import GitResult, GitRunner

logger = logging.getLogger("daiv.tools")


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Only the opening and closing markers: a bare ``=======`` is also a setext heading underline.
_CONFLICT_MARKER_PATTERN = r"^(<{7}|>{7})( |$)"


@dataclass(frozen=True)
class RepoStatus:
    """One-shot snapshot of the run's repo state, gathered in <=2 sandbox round-trips."""

    dirty: bool
    diff: str
    remote_branches: list[str]
    has_unpushed: bool


@dataclass(frozen=True)
class PendingMerge:
    """A merge the agent started with ``git merge --no-commit`` and left for the publisher to commit."""

    head: str
    """The merged commit (``MERGE_HEAD``)."""
    branch: str | None
    """The branch ``head`` is the tip of, as ``git name-rev`` names it (``remotes/origin/main``, ``main``); ``None``
    when it is no branch's tip."""
    unmerged_paths: tuple[str, ...]
    """Files the merge left unmerged. The agent cannot stage, so the ones it resolved are still listed."""
    conflicted_paths: tuple[str, ...]
    """The ``unmerged_paths`` that still hold a ``<<<<<<<`` or ``>>>>>>>`` conflict marker line."""

    def commit_subject(self, into: str | None) -> str:
        """The subject git gives this merge's commit, naming the branch it lands on (``into``) when there is one."""
        if self.branch is None:
            subject = f"Merge commit '{self.head}'"
        elif self.branch.startswith("remotes/"):
            subject = f"Merge remote-tracking branch '{self.branch.removeprefix('remotes/')}'"
        else:
            subject = f"Merge branch '{self.branch}'"
        return subject if into is None else f"{subject} into {into}"


class GitManager:
    """Run git operations against the run's repository through a :class:`~automation.agent.git_runners.GitRunner`.

    Args:
        runner: Where the git commands run.
    """

    def __init__(self, runner: GitRunner) -> None:
        self._runner = runner

    # -- git invocation ------------------------------------------------------
    async def _git(self, *args: str, check: bool = True) -> GitResult:
        """Run one git command in the repo. Raises ``GitCommandError`` on a non-zero
        exit when ``check`` is True; otherwise returns the result for the caller to inspect.
        """
        result = await self._runner.run(args)
        if check and result.exit_code != 0:
            raise GitCommandError(["git", *args], result.exit_code, result.output)
        return result

    async def _git_batch(self, commands: list[tuple[str, ...]]) -> list[GitResult]:
        """Run several git commands, in one round-trip where the runner allows it; results in input order.

        Never raises on a non-zero exit — callers inspect each result's exit_code, mirroring the
        per-command ``check=False`` discipline.
        """
        if not commands:
            return []
        return await self._runner.run_batch(commands)

    @staticmethod
    def _append_untracked(diff: str, files: list[str], results: list[GitResult]) -> str:
        """Fold per-untracked-file ``diff --no-index`` results into ``diff`` and normalise the trailing newline.

        ``diff --no-index`` exits 1 when it finds differences (expected — keep the output); exit >1 is a
        genuine error and must surface.

        Sections are butted directly together (ensuring a single ``\\n`` boundary), never separated by a
        blank line — the output must match what one ``git diff`` invocation emits. A blank line between
        sections is not just cosmetic: when the section it follows is *hunkless* (an empty-file addition,
        ``diff --git`` + ``new file mode`` + ``index`` with no ``@@``), ``unidiff`` aborts the entire parse
        with "Unexpected trailing newline character". That silently bypasses ``redact_diff_content``'s
        omit-pattern redaction, leaking excluded file content to the diff-to-metadata model.
        """
        for fres, f in zip(results, files, strict=True):
            if fres.exit_code > 1:
                raise GitCommandError(["git", "diff", "--no-index", "/dev/null", f], fres.exit_code, fres.output)
            if fres.output:
                if diff and not diff.endswith("\n"):
                    diff += "\n"
                diff += fres.output
        if diff and not diff.endswith("\n"):
            diff += "\n"
        return diff

    @staticmethod
    def _require_ok(args: tuple[str, ...], res: GitResult) -> GitResult:
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

    async def pending_merge(self) -> PendingMerge | None:
        """The merge in progress, or ``None``. One round-trip, and a second when the merge left unmerged files.

        Only ``rev-parse``'s "no such ref" exit means no merge; any other failure raises, since reading it as "no merge"
        would commit the merge as a plain commit without checking it for conflict markers.
        """
        specs: list[tuple[str, ...]] = [
            ("rev-parse", "-q", "--verify", "MERGE_HEAD"),
            ("name-rev", "--name-only", "--no-undefined", "--exclude=*/HEAD", "--exclude=refs/tags/*", "MERGE_HEAD"),
            ("ls-files", "-u", "-z"),
        ]
        head_res, name_res, unmerged_res = await self._git_batch(specs)
        if head_res.exit_code == 1:
            return None
        head = self._sha_from(specs[0], self._require_ok(specs[0], head_res))
        self._require_ok(specs[2], unmerged_res)
        entries = unmerged_res.output.split("\0")
        unmerged = tuple(sorted({entry.split("\t", 1)[1] for entry in entries if "\t" in entry}))
        conflicted: tuple[str, ...] = ()
        if unmerged:
            grep_args = ("--literal-pathspecs", "grep", "-l", "-z", "-E", _CONFLICT_MARKER_PATTERN, "--", *unmerged)
            grep_res = await self._git(*grep_args, check=False)
            if grep_res.exit_code not in (0, 1):
                self._require_ok(grep_args, grep_res)
            # The output interleaves stderr, so a warning can lead the first path's chunk.
            hits = {chunk.rsplit("\n", 1)[-1] for chunk in grep_res.output.split("\0")}
            conflicted = tuple(path for path in unmerged if path in hits)
        names = self._nonempty_lines(name_res.output) if name_res.exit_code == 0 else []
        branch = names[-1] if names and not any(char in names[-1] for char in "~^") else None
        return PendingMerge(head=head, branch=branch, unmerged_paths=unmerged, conflicted_paths=conflicted)

    async def status_snapshot(
        self, *, base_branch: str, mr_source_branch: str | None, merge_head: str | None = None
    ) -> RepoStatus:
        """Collect everything the publisher needs in exactly two sandbox round-trips.

        Batch A: working-tree status, merge-base of ``origin/<base_branch>`` and ``HEAD``, untracked
        file list, remote branch list, and — when ``mr_source_branch`` is given —
        ``origin/<mr_source_branch>..HEAD``. Batch B (always): the working-tree diff against the
        resolved merge-base SHA (or the branch tip when there is no common ancestor), plus one
        ``diff --no-index`` per untracked file. Replaces the previous is_dirty/get_diff/has_unpushed/
        remote_branches sequence (~5+U round-trips) with exactly 2. Diffing the merge-base (rather
        than the branch tip) yields the branch's own delta only; tip diffing sweeps unrelated commits
        in when the branch is stacked off a moving target.

        ``merge_head``: the commit a pending merge brings in. The merge-base is then taken against the
        merge commit the publisher is about to make, so the merged branch's own changes stay out of the diff.
        """
        batch_a: list[tuple[str, ...]] = [
            ("status", "--porcelain"),
            ("merge-base", f"origin/{base_branch}", "HEAD", *([merge_head] if merge_head else [])),
            ("ls-files", "--others", "--exclude-standard"),
            ("ls-remote", "--heads", "origin"),
        ]
        log_idx = -1
        if mr_source_branch:
            batch_a.append(("log", f"origin/{mr_source_branch}..HEAD", "--oneline"))
            log_idx = len(batch_a) - 1

        res = await self._git_batch(batch_a)
        status_res, mergebase_res, untracked_res, lsremote_res = res[0], res[1], res[2], res[3]

        # status/ls-files use the same exit-code discipline as before.
        self._require_ok(batch_a[0], status_res)
        self._require_ok(batch_a[2], untracked_res)

        # merge-base is not gated like the others: exit 1 ("no common ancestor") is expected for
        # unrelated histories, so fall back to the base tip; any other non-zero exit is a real failure.
        base_ref = f"origin/{base_branch}"
        if mergebase_res.exit_code == 0 and mergebase_res.output.strip():
            base_ref = mergebase_res.output.strip().splitlines()[0]
        elif mergebase_res.exit_code == 1:
            logger.warning(
                "status_snapshot: no common ancestor between origin/%s and HEAD; "
                "falling back to the branch tip for the diff base.",
                base_branch,
            )
        else:
            self._require_ok(batch_a[1], mergebase_res)  # exit 128 (bad ref) raises; exit-0-no-SHA keeps the tip

        # On a local runner ls-remote is the publish's first network op, so a bad credential fails here:
        # classify it as a push would. Non-transport failures fall through to _require_ok.
        if lsremote_res.exit_code != 0:
            _raise_for_transport_failure(list(batch_a[3]), lsremote_res)
        self._require_ok(batch_a[3], lsremote_res)

        # Batch B: the working-tree diff against the resolved base, plus one diff --no-index per
        # untracked file. The main diff depends on the merge-base from batch A, so batch B always runs.
        untracked = self._nonempty_lines(untracked_res.output)
        diff_specs: list[tuple[str, ...]] = [("diff", base_ref)]
        diff_specs += [("diff", "--no-index", "/dev/null", f) for f in untracked]
        batch_b = await self._git_batch(diff_specs)
        self._require_ok(diff_specs[0], batch_b[0])
        diff = self._append_untracked(batch_b[0].output, untracked, batch_b[1:])

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

    async def head_sha(self) -> str:
        """The full sha of the current ``HEAD``.

        ``GitResult.output`` is stdout *and* stderr, so a benign git warning (an unreadable
        ``.gitconfig``, a gc hint) would otherwise be returned as part of the sha; see :meth:`_sha_from`.
        """
        return self._sha_from(("rev-parse", "HEAD"), await self._git("rev-parse", "HEAD"))

    @classmethod
    def _sha_from(cls, args: tuple[str, ...], result: GitResult) -> str:
        """The full sha a ``rev-parse`` printed: its last line, refusing anything that is not a full sha rather than
        hand a caller one a git warning poisoned."""
        lines = cls._nonempty_lines(result.output)
        sha = lines[-1] if lines else ""
        if not _FULL_SHA_RE.fullmatch(sha):
            raise GitCommandError(["git", *args], result.exit_code, stderr=result.output)
        return sha

    async def push_head_to(
        self, branch: str, *, force: bool = False, integrate_on_reject: bool = False, skip_ci: bool = False
    ) -> str:
        """Push the current ``HEAD`` to ``origin/<branch>`` (creating it if needed).

        When ``integrate_on_reject`` is set and a *non-fast-forward* rejection comes back — the
        remote branch advanced under the run, e.g. a dependabot force-push of its rebased PR
        branch, or a concurrent human push to an MR's source branch — the manager fetches the
        remote tip, rebases ``HEAD`` onto it or merges it in (see :meth:`_integrate_remote`), and
        retries the push once. This preserves the agent's work instead of discarding it. Pass it only
        when adding onto a branch is the intent (an existing MR's source branch); a fresh-branch push
        leaves it off so we never graft the run's commits onto unrelated history that happens to
        occupy a colliding ref.

        Raises ``GitPushStaleError`` on a non-fast-forward rejection that cannot be (or was asked not
        to be) integrated — including a rebase or merge conflict or a remote that advanced again before the
        retry. Raises ``GitPushPermissionError`` on an auth/permission failure, ``GitPushNetworkError``
        when the remote host is unreachable (e.g. a network-disabled sandbox), and ``GitCommandError``
        on any other push failure. Returns ``branch``.

        skip_ci: pass ``-o ci.skip`` so the push runs no jobs. GitLab still records a jobless
            ``skipped`` pipeline for it, and a webhook for that row (see ``PipelineReport.is_judgeable``).
        """
        push_args = [
            "push",
            *(["-o", "ci.skip"] if skip_ci else []),
            "origin",
            f"HEAD:{branch}",
            *(["--force"] if force else []),
        ]
        push = await self._git(*push_args, check=False)
        if push.exit_code == 0:
            return branch

        # A non-fast-forward rejection is the only failure that integrating remote work can fix; an
        # auth/network/other failure won't change after a fetch and integration, so it falls straight through
        # to classification below. On a successful integrate, retry the push and return on success.
        if integrate_on_reject and not force and _is_push_stale_error_text(push.output):
            await self._integrate_remote(branch)  # raises GitPushStaleError on a conflict
            push = await self._git(*push_args, check=False)
            if push.exit_code == 0:
                return branch

        # Either the push failed and we couldn't/didn't integrate, or the retry was still rejected
        # (remote advanced again) — classify whichever failed push result we are holding.
        _raise_for_push_failure(push_args, push)

    async def _integrate_remote(self, branch: str) -> None:
        """Fetch ``origin/<branch>`` and build ``HEAD`` on top of it so a non-fast-forward push can retry.

        On success ``HEAD`` is the run's commits replayed on top of the latest remote tip. When the run's own commits,
        the ones on no ``origin`` ref, include a merge, the remote tip is merged in instead: a rebase would re-create
        the merged branch's commits under new shas, so the merge request would show that branch's changes as its own
        again. A failed fetch is classified by transport (auth → ``GitPushPermissionError``, unreachable host →
        ``GitPushNetworkError``, else ``GitCommandError``) — via the operation-neutral
        :func:`_raise_for_transport_failure` rather than the push classifier, so a fetch never raises a nonsensical
        non-fast-forward error — keeping the actionable typed error the direct push would have produced instead of
        degrading to a raw ``GitCommandError``. On a conflict the rebase or merge is aborted (restoring the earlier
        ``HEAD`` rather than stranding the workspace mid-way) and a typed :class:`GitPushStaleError` is raised. A
        *failed* abort cannot restore ``HEAD``: it is logged at error level (never silently swallowed) and flagged in
        the raised message, since the workspace is then left mid-way and must not be re-published.
        """
        # Before the fetch moves origin/<branch>: after a force-push, the fetched tip no longer holds the old history.
        merges = await self._git("rev-list", "--merges", "HEAD", "--not", "--remotes=origin")
        integrate_args = ("merge", "--no-edit", "FETCH_HEAD") if merges.output.strip() else ("rebase", "FETCH_HEAD")
        operation = integrate_args[0]

        fetch_args = ["fetch", "origin", branch]
        fetch = await self._git(*fetch_args, check=False)
        if fetch.exit_code != 0:
            _raise_for_transport_failure(fetch_args, fetch)
            raise GitCommandError(["git", *fetch_args], fetch.exit_code, fetch.output)

        integrate = await self._git(*integrate_args, check=False)
        if integrate.exit_code != 0:
            logger.warning(
                "git %s FETCH_HEAD failed on '%s' (exit %s): %s",
                operation,
                branch,
                integrate.exit_code,
                integrate.output.strip(),
            )
            abort = await self._git(operation, "--abort", check=False)
            if abort.exit_code != 0:
                logger.error(
                    "git %s --abort failed after a conflict on '%s' (exit %s); the workspace is left mid-%s. "
                    "Output: %s",
                    operation,
                    branch,
                    abort.exit_code,
                    operation,
                    abort.output.strip(),
                )
                raise GitPushStaleError(
                    f"The remote branch '{branch}' moved while DAIV was working and its changes conflict "
                    f"with DAIV's. The conflicted {operation} could not be aborted, so the workspace is in an "
                    "inconsistent state. Re-trigger DAIV to retry from a fresh clone."
                )
            raise GitPushStaleError(
                f"The remote branch '{branch}' moved while DAIV was working and its changes conflict "
                "with DAIV's, so they could not be integrated automatically. Re-trigger DAIV to retry "
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

    def unique_branch_name(self, original_branch_name: str, existing_branch_names: list[str]) -> str:
        """
        Generate a branch name that does not collide with an existing remote branch.

        Returns ``original_branch_name`` untouched when it is free; otherwise appends a
        random suffix. A random suffix (rather than an incrementing counter) means naming
        can never exhaust and abort the publish, which would discard the agent's work.

        Args:
            original_branch_name: The preferred branch name.
            existing_branch_names: Remote branch names to avoid colliding with.

        Returns:
            A branch name absent from ``existing_branch_names``.
        """
        existing = set(existing_branch_names)
        branch_name = original_branch_name

        while branch_name in existing:
            branch_name = f"{original_branch_name}-{uuid4().hex[:8]}"

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
    fetch and integration, and real auth/network output does not contain these non-fast-forward markers.
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


def _raise_for_transport_failure(args: list[str], result: GitResult) -> None:
    """Raise a typed error for an auth/permission or unreachable-host git failure; return otherwise.

    Operation-neutral on purpose: a credential or host-reachability problem (and its remedy) is the
    same whether the failing command was a ``push`` or the ``fetch`` of a push-recovery integration, so
    both share this classification. Returns (does not raise) when the output matches neither marker,
    leaving the caller to layer operation-specific classification (e.g. push's non-fast-forward
    branch) on top. Auth is checked before network so an auth failure that also names a host wins.
    """
    if is_git_auth_error_text(result.output):
        logger.warning("git transport auth failure: %s", result.output)
        raise GitPushPermissionError(
            "Failed to authenticate to the remote repository (authentication or permission issue). "
            "The short-lived credential used for this remote operation may be expired (a session "
            "resumed a day or more after it was created holds an expired clone token — a fresh session "
            "re-clones with a new one), it may not have been sent to the remote, or branch protection "
            "rules may not allow this credential to write to this branch."
        )
    if _is_push_network_error_text(result.output):
        logger.warning("git transport network failure: %s", result.output)
        raise GitPushNetworkError(
            "Failed to reach the remote host (it is unreachable). DAIV runs git from inside the sandbox, "
            "so the sandbox environment must run as an egress-enabled sandbox."
        )


def _raise_for_push_failure(push_args: list[str], result: GitResult) -> NoReturn:
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
