from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from git import GitCommandError

from codebase.clients.checkout import PublishBlock
from codebase.utils import is_git_auth_error_text

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import NoReturn

    from automation.agent.workspace.git_runners import GitResult, GitRunner
    from codebase.repo_config import Checkout

logger = logging.getLogger("daiv.tools")


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

FastForward = Literal["advanced", "replaced", "unchanged", "diverged"]
# Only the opening and closing markers: a bare ``=======`` is also a setext heading underline.
_CONFLICT_MARKER_PATTERN = r"^(<{7}|>{7})( |$)"

KEPT_OUT_FILES_CAP = 20
_GITLINK_MODE = "160000"
_NO_MODE = "000000"
_LFS_ONLY = ":(attr:filter=lfs)"
_WITHOUT_LFS = (".", ":(exclude,attr:filter=lfs)")
_NO_RECORD: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class PublishScope:
    """What a publish keeps out of its commit, and the submodule commits it records instead (research R8, R16).

    ``submodules`` keeps every gitlink and every change inside a submodule out; ``lfs`` keeps every LFS-tracked path
    out. ``record`` maps a direct child submodule's path to the commit the commit records for it: the only gitlinks a
    commit may change. ``PublishScope()`` sends exactly the commands a publish sent before submodules and LFS.
    """

    submodules: bool = False
    lfs: bool = False
    record: Mapping[str, str] = field(default=_NO_RECORD)

    @classmethod
    def from_config(cls, checkout: Checkout, *, record: Mapping[str, str] | None = None) -> PublishScope:
        return cls(submodules=checkout.submodules, lfs=checkout.lfs, record=MappingProxyType(dict(record or {})))

    @property
    def is_default(self) -> bool:
        return not (self.submodules or self.lfs or self.record)

    @property
    def flags(self) -> tuple[str, ...]:
        """``status`` and ``diff`` options: changes inside submodules and moved gitlinks stay out."""
        return ("--ignore-submodules=all",) if self.submodules else ()

    @property
    def pathspec(self) -> tuple[str, ...]:
        """A trailing pathspec that leaves LFS-tracked paths out."""
        return ("--", *_WITHOUT_LFS) if self.lfs else ()


@dataclass(frozen=True)
class KeptOutSubmodule:
    """A submodule whose changes a publish left out. ``removed`` is a submodule deleted or moved, ``added`` a gitlink or
    an embedded repository the agent created, ``remote_moved`` a recorded commit the remote branch moved meanwhile
    (research R20). ``changed_files`` holds at most ``KEPT_OUT_FILES_CAP`` paths, ``more`` counts the rest."""

    path: str
    commit_moved: bool = False
    removed: bool = False
    added: bool = False
    remote_moved: bool = False
    block: PublishBlock | None = None
    changed_files: tuple[str, ...] = ()
    more: int = 0

    def to_state(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "commit_moved": self.commit_moved,
            "removed": self.removed,
            "added": self.added,
            "remote_moved": self.remote_moved,
            "block": self.block.value if self.block is not None else None,
            "changed_files": list(self.changed_files),
            "more": self.more,
        }

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> KeptOutSubmodule:
        return cls(
            path=data["path"],
            commit_moved=bool(data.get("commit_moved")),
            removed=bool(data.get("removed")),
            added=bool(data.get("added")),
            remote_moved=bool(data.get("remote_moved")),
            block=PublishBlock(data["block"]) if data.get("block") else None,
            changed_files=tuple(data.get("changed_files") or ()),
            more=int(data.get("more") or 0),
        )


@dataclass(frozen=True)
class KeptOut:
    """What a publish left out of its commit (FR-013); falsy when nothing was."""

    lfs_paths: tuple[str, ...] = ()
    submodules: tuple[KeptOutSubmodule, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.lfs_paths or self.submodules)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path ``commit_all`` must leave as it is in ``HEAD``."""
        return (*self.lfs_paths, *(sub.path for sub in self.submodules if not sub.remote_moved))

    def to_state(self) -> dict[str, Any]:
        return {"lfs_paths": list(self.lfs_paths), "submodules": [sub.to_state() for sub in self.submodules]}

    @classmethod
    def from_state(cls, data: Mapping[str, Any]) -> KeptOut:
        return cls(
            lfs_paths=tuple(data.get("lfs_paths") or ()),
            submodules=tuple(KeptOutSubmodule.from_state(sub) for sub in data.get("submodules") or ()),
        )


@dataclass(frozen=True)
class RepoStatus:
    """One-shot snapshot of the run's repo state, gathered in <=2 sandbox round-trips."""

    dirty: bool
    diff: str
    """The branch's whole delta: the working tree against its merge-base with the base branch."""
    uncommitted_diff: str | None
    """The working tree against ``HEAD``: what ``commit_all`` would commit. ``None`` while a merge is pending."""
    remote_branches: list[str]
    has_unpushed: bool
    kept_out: KeptOut = KeptOut()
    """What the publish's scope keeps out of the commit; empty under ``PublishScope()``."""


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
    """Run git operations against the run's repository through a :class:`~.git_runners.GitRunner`.

    Args:
        runner: Where the git commands run.
    """

    def __init__(self, runner: GitRunner) -> None:
        self._runner = runner
        self.remote_moved: tuple[str, ...] = ()
        """The recorded submodule paths whose commit the last ``push_head_to`` took from the remote (research R20)."""

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
        self,
        *,
        base_branch: str,
        mr_source_branch: str | None,
        merge_head: str | None = None,
        scope: PublishScope = PublishScope(),  # noqa: B008
    ) -> RepoStatus:
        """Collect everything the publisher needs in exactly two sandbox round-trips.

        Batch A: working-tree status, merge-base of ``origin/<base_branch>`` and ``HEAD``, untracked
        file list, remote branch list, ``HEAD``'s SHA, and — when ``mr_source_branch`` is given —
        ``origin/<mr_source_branch>..HEAD``. Batch B (always): the working-tree diff against the
        resolved merge-base SHA (or the branch tip when there is no common ancestor), the one against
        ``HEAD`` unless it would be empty, unused or the same as the first, plus one
        ``diff --no-index`` per untracked file. Replaces the previous is_dirty/get_diff/has_unpushed/
        remote_branches sequence (~5+U round-trips) with exactly 2. Diffing the merge-base (rather
        than the branch tip) yields the branch's own delta only; tip diffing sweeps unrelated commits
        in when the branch is stacked off a moving target.

        ``merge_head``: the commit a pending merge brings in. The merge-base is then taken against the
        merge commit the publisher is about to make, so the merged branch's own changes stay out of the diff.

        ``scope`` leaves submodules and LFS-tracked paths out of ``dirty`` and the diffs, and lists them in
        ``kept_out`` instead (batch A reads them, batch B the files changed inside each submodule). A ``scope.record``
        entry whose commit differs from the index counts as a change, and adds a ``Submodule <path> <a>..<b>:`` summary
        of the commits it brings to the diffs.
        """
        flags, spec = scope.flags, scope.pathspec
        batch_a: list[tuple[str, ...]] = [
            ("status", "--porcelain", *flags, *spec),
            ("merge-base", f"origin/{base_branch}", "HEAD", *([merge_head] if merge_head else [])),
            ("ls-files", "--others", "--exclude-standard", *spec),
            ("ls-remote", "--heads", "origin"),
            ("rev-parse", "HEAD"),
        ]
        log_idx = -1
        if mr_source_branch:
            batch_a.append(("log", f"origin/{mr_source_branch}..HEAD", "--oneline"))
            log_idx = len(batch_a) - 1
        submodules_idx = lfs_idx = record_idx = -1
        if scope.submodules:
            batch_a.append(("status", "--porcelain=v2", "-z"))
            submodules_idx = len(batch_a) - 1
        if scope.lfs:
            batch_a.append(("status", "--porcelain", "-z", "--untracked-files=all", "--", _LFS_ONLY))
            lfs_idx = len(batch_a) - 1
        if scope.record:
            batch_a.append(("--literal-pathspecs", "ls-files", "-s", "-z", "--", *scope.record))
            record_idx = len(batch_a) - 1

        res = await self._git_batch(batch_a)
        status_res, mergebase_res, untracked_res, lsremote_res, head_res = res[:5]

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
        head = self._sha_from(batch_a[4], self._require_ok(batch_a[4], head_res))

        untracked = self._nonempty_lines(untracked_res.output)
        embedded: list[str] = []
        if scope.submodules:
            # ls-files lists a directory holding its own repository as ``dir/`` and never looks inside it.
            embedded = [path.removesuffix("/") for path in untracked if path.endswith("/")]
            untracked = [path for path in untracked if not path.endswith("/")]
        kept_submodules: list[KeptOutSubmodule] = []
        with_files: set[str] = set()
        if submodules_idx >= 0:
            v2 = self._require_ok(batch_a[submodules_idx], res[submodules_idx]).output
            kept_submodules, with_files = _parse_submodule_changes(v2, record=scope.record)
            kept_submodules += [KeptOutSubmodule(path=path, added=True) for path in embedded]
        lfs_paths: tuple[str, ...] = ()
        if lfs_idx >= 0:
            lfs_paths = _parse_porcelain_paths(self._require_ok(batch_a[lfs_idx], res[lfs_idx]).output)
        recorded_from: dict[str, str] = {}
        if record_idx >= 0:
            index = _parse_index_gitlinks(self._require_ok(batch_a[record_idx], res[record_idx]).output)
            recorded_from = {
                path: index.get(path, "0" * 40) for path, sha in scope.record.items() if index.get(path) != sha
            }

        # Batch B: the working-tree diffs, plus one diff --no-index per untracked file. The main diff depends on the
        # merge-base from batch A, so batch B always runs.
        if scope.is_default:
            dirty = bool(status_res.output.strip())
        else:
            tracked = [line for line in status_res.output.splitlines() if line.strip() and not line.startswith("??")]
            dirty = bool(tracked or untracked or recorded_from)
        diffs_head = dirty and merge_head is None and base_ref != head
        diff_specs: list[tuple[str, ...]] = [
            ("diff", *flags, base_ref, *spec),
            *([("diff", *flags, "HEAD", *spec)] if diffs_head else []),
        ]
        diff_specs += [("diff", "--no-index", "/dev/null", f) for f in untracked]
        inside = [sub for sub in kept_submodules if sub.path in with_files]
        inside_specs = [("-C", sub.path, "status", "--porcelain", "-z") for sub in inside]
        record_specs = [
            ("-C", path, "log", "--format=  > %s", f"{old}..{scope.record[path]}")
            for path, old in recorded_from.items()
        ]
        batch_b = await self._git_batch(diff_specs + inside_specs + record_specs)
        base_diff_res, *other_diff_res = batch_b[: len(diff_specs)]
        inside_res = batch_b[len(diff_specs) : len(diff_specs) + len(inside_specs)]
        record_res = batch_b[len(diff_specs) + len(inside_specs) :]
        self._require_ok(diff_specs[0], base_diff_res)
        head_diff_res = other_diff_res.pop(0) if diffs_head else None
        diff = self._append_untracked(base_diff_res.output, untracked, other_diff_res)
        uncommitted_diff: str | None
        if head_diff_res is not None:
            self._require_ok(diff_specs[1], head_diff_res)
            uncommitted_diff = self._append_untracked(head_diff_res.output, untracked, other_diff_res)
        elif merge_head:
            uncommitted_diff = None
        elif not dirty:
            uncommitted_diff = ""
        else:
            uncommitted_diff = diff  # HEAD is the merge-base

        if recorded_from:
            summary = _submodule_summary(recorded_from, scope.record, record_res)
            diff += summary
            if uncommitted_diff is not None:
                uncommitted_diff += summary
        if inside:
            changed = {
                sub.path: _parse_porcelain_paths(result.output) if result.exit_code == 0 else ()
                for sub, result in zip(inside, inside_res, strict=True)
            }
            kept_submodules = [
                _with_changed_files(sub, changed[sub.path]) if sub.path in changed else sub for sub in kept_submodules
            ]

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
            dirty=dirty,
            diff=diff,
            uncommitted_diff=uncommitted_diff,
            remote_branches=self._parse_remote_branches(lsremote_res.output),
            has_unpushed=has_unpushed,
            kept_out=KeptOut(lfs_paths=lfs_paths, submodules=tuple(kept_submodules)),
        )

    # -- mutations -----------------------------------------------------------
    async def commit_all(
        self, message: str, *, keep_out: Sequence[str] = (), record: Mapping[str, str] | None = None
    ) -> bool:
        """Stage every change and commit it; return whether a commit was made. Callers should ensure the tree is dirty
        first (``git commit`` exits non-zero on an empty index).

        ``keep_out`` paths stay as they are in ``HEAD``. With ``record`` (a publish that handles submodules), every
        gitlink ``git add -A`` staged that ``record`` doesn't name stays as in ``HEAD`` too, and each ``record`` entry
        is written as its gitlink's commit. When nothing is left to commit after that, no commit is made.
        """
        await self._git("add", "-A")
        if not keep_out and record is None:
            await self._git("commit", "-m", message)
            return True
        if keep_out:
            await self._git("--literal-pathspecs", "reset", "-q", "--", *keep_out)
        if record is not None:
            raw = await self._git("diff", "--cached", "--raw", "-z", "--no-renames")
            if stray := [path for path in _parse_raw_gitlinks(raw.output) if path not in record]:
                await self._git("--literal-pathspecs", "reset", "-q", "--", *stray)
            for path, sha in record.items():
                await self._git("update-index", "--cacheinfo", f"{_GITLINK_MODE},{sha},{path}")
        staged = await self._git("diff", "--cached", "--quiet", check=False)
        if staged.exit_code == 0:
            logger.info("Nothing left to commit once the changes a publish keeps out were left out")
            return False
        if staged.exit_code != 1:
            raise GitCommandError(["git", "diff", "--cached", "--quiet"], staged.exit_code, staged.output)
        await self._git("commit", "-m", message)
        return True

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

    async def fast_forward(self, branch: str, *, scope: PublishScope = PublishScope()) -> FastForward:  # noqa: B008
        """Move ``HEAD`` up to ``origin/<branch>`` when the remote moved and the checkout has nothing unpublished.

        Changes ``scope`` keeps out of a publish don't count as unpublished.

        ``"advanced"`` when it fast-forwarded. ``"replaced"`` when the remote branch was rewritten (a rebase or a
        force-push) and the checkout held nothing unpublished (:meth:`_holds_nothing_unpublished`), so it was reset to
        the new head. ``"unchanged"`` when the remote is where ``HEAD`` is (or could not be read: the run then works
        from what it has, and the push integrates later). ``"diverged"`` when ``HEAD`` holds commits no ``origin`` ref
        had, or the tree's changes or a pending merge stand in the way; the checkout is then left as it was.
        """
        # Before the fetch moves origin/<branch>: a rewritten head no longer holds the commits HEAD was published as.
        # The fetch stays out of the batches: a local runner runs a batch's commands concurrently.
        read_head = ("rev-parse", "HEAD")
        head, status, unpushed, merge = await self._git_batch([
            read_head,
            ("status", "--porcelain", *scope.flags, *scope.pathspec),
            ("rev-list", "-n1", "HEAD", "--not", "--remotes=origin"),
            ("rev-parse", "-q", "--verify", "MERGE_HEAD"),
        ])
        published = self._holds_nothing_unpublished(status, unpushed, merge)
        fetch = await self._git("fetch", "origin", branch, check=False)
        if fetch.exit_code != 0:
            logger.warning("Could not fetch origin/%s to fast-forward (exit %s)", branch, fetch.exit_code)
            return "unchanged"
        read_remote = ("rev-parse", "FETCH_HEAD")
        fetched, ancestor = await self._git_batch([read_remote, ("merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")])
        remote = self._sha_from(read_remote, self._require_ok(read_remote, fetched))
        if remote == self._sha_from(read_head, self._require_ok(read_head, head)):
            return "unchanged"
        if ancestor.exit_code == 0:
            ff = await self._git("merge", "--ff-only", "FETCH_HEAD", check=False)
            return "advanced" if ff.exit_code == 0 else "diverged"
        if not published:
            return "diverged"
        reset = await self._git("reset", "--keep", "FETCH_HEAD", check=False)
        if reset.exit_code != 0:
            logger.warning("Could not move the checkout to the rewritten origin/%s: %s", branch, reset.output.strip())
            return "diverged"
        return "replaced"

    @staticmethod
    def _holds_nothing_unpublished(status: GitResult, unpushed: GitResult, merge: GitResult) -> bool:
        """Whether moving the checkout off ``HEAD`` loses nothing: a clean tree (``status --porcelain``), every commit
        of ``HEAD`` on an ``origin`` ref (``rev-list -n1 HEAD --not --remotes=origin``) and no pending merge
        (``rev-parse -q --verify MERGE_HEAD``). A command that failed counts as holding something."""
        clean = status.exit_code == 0 and not status.output.strip()
        pushed = unpushed.exit_code == 0 and not unpushed.output.strip()
        return clean and pushed and merge.exit_code == 1

    async def push_head_to(
        self,
        branch: str,
        *,
        force: bool = False,
        integrate_on_reject: bool = False,
        skip_ci: bool = False,
        scope: PublishScope = PublishScope(),  # noqa: B008
        from_detached: bool = False,
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
        scope: its ``record`` names the submodule commits the run's commits record; an integration that conflicts only
            on those takes the remote's commits for them (``remote_moved``) instead of failing.
        from_detached: ``HEAD`` names no branch, as a submodule's first publish has it; git then needs the full
            ``refs/heads/<branch>`` to create the branch.
        """
        self.remote_moved = ()
        push_args = [
            "push",
            *(["-o", "ci.skip"] if skip_ci else []),
            "origin",
            f"HEAD:refs/heads/{branch}" if from_detached else f"HEAD:{branch}",
            *(["--force"] if force else []),
        ]
        push = await self._git(*push_args, check=False)
        if push.exit_code == 0:
            return branch

        # A non-fast-forward rejection is the only failure that integrating remote work can fix; an
        # auth/network/other failure won't change after a fetch and integration, so it falls straight through
        # to classification below. On a successful integrate, retry the push and return on success.
        if integrate_on_reject and not force and _is_push_stale_error_text(push.output):
            self.remote_moved = await self._integrate_remote(branch, scope)  # raises GitPushStaleError on a conflict
            push = await self._git(*push_args, check=False)
            if push.exit_code == 0:
                return branch

        # Either the push failed and we couldn't/didn't integrate, or the retry was still rejected
        # (remote advanced again) — classify whichever failed push result we are holding.
        _raise_for_push_failure(push_args, push)

    async def _integrate_remote(self, branch: str, scope: PublishScope = PublishScope()) -> tuple[str, ...]:  # noqa: B008
        """Fetch ``origin/<branch>`` and build ``HEAD`` on top of it so a non-fast-forward push can retry.

        A conflict only on ``scope.record`` gitlinks is resolved to the remote's commits, since the remote's pointer
        wins (FR-014); their paths are returned. Any other conflict aborts.

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
        if integrate.exit_code == 0:
            return ()
        if scope.record and (moved := await self._resolve_recorded_conflicts(operation, scope.record)) is not None:
            logger.info("The remote branch '%s' moved %s too; kept the remote's commits", branch, ", ".join(moved))
            return moved
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
                "git %s --abort failed after a conflict on '%s' (exit %s); the workspace is left mid-%s. Output: %s",
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

    async def _resolve_recorded_conflicts(self, operation: str, record: Mapping[str, str]) -> tuple[str, ...] | None:
        """Take the remote's commit for each conflicted gitlink of ``record`` and finish the stopped ``operation``;
        ``None`` as soon as a conflict touches anything else. A rebased commit the resolution leaves empty is
        dropped."""
        # During a rebase stage 2 is the upstream being rebased onto; during a merge stage 3 is FETCH_HEAD.
        stage = "2" if operation == "rebase" else "3"
        moved: set[str] = set()
        while True:
            unmerged = _parse_unmerged((await self._git("ls-files", "-u", "-z", check=False)).output)
            if not unmerged or not set(unmerged) <= set(record):
                return None
            for path, stages in unmerged.items():
                if (sha := stages.get(stage)) is None:
                    return None
                await self._git("update-index", "--cacheinfo", f"{_GITLINK_MODE},{sha},{path}")
                moved.add(path)
            if operation == "merge":
                commit = await self._git("commit", "--no-edit", check=False)
                return tuple(sorted(moved)) if commit.exit_code == 0 else None
            empty = (await self._git("diff", "--cached", "--quiet", check=False)).exit_code == 0
            step = await self._git("-c", "core.editor=true", "rebase", "--skip" if empty else "--continue", check=False)
            if step.exit_code == 0:
                return tuple(sorted(moved))

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


def _is_push_workflow_permission_error_text(output: str) -> bool:
    """Check if git push output is GitHub refusing a change under ``.github/workflows/`` because the token
    lacks the ``workflows`` permission (a ``[remote rejected]``, so it never carries an auth marker)."""
    return "to create or update workflow" in output.lower()


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
    via :func:`_raise_for_transport_failure`, checked first so they win); GitHub refusing a change to a
    workflow file → ``GitPushPermissionError``; a non-fast-forward rejection → ``GitPushStaleError``;
    anything else → the raw ``GitCommandError``.
    """
    _raise_for_transport_failure(push_args, result)
    if _is_push_workflow_permission_error_text(result.output):
        logger.warning("git push rejected for a workflow change: %s", result.output)
        raise GitPushPermissionError(
            "GitHub rejected the push because it changes a file under .github/workflows/ and DAIV's token "
            "lacks the workflows permission. Grant the GitHub App the Workflows: Read and write repository "
            "permission, accept it on the installation, then re-trigger DAIV."
        )
    if _is_push_stale_error_text(result.output):
        logger.warning("git push non-fast-forward rejection: %s", result.output)
        raise GitPushStaleError(
            "Failed to push changes: the remote branch advanced while DAIV was working (a non-fast-forward "
            "rejection). DAIV could not integrate the remote changes automatically. Re-trigger DAIV to "
            "retry against the updated branch."
        )
    raise GitCommandError(["git", *push_args], result.exit_code, result.output)


def _parse_submodule_changes(output: str, *, record: Mapping[str, str]) -> tuple[list[KeptOutSubmodule], set[str]]:
    """The submodule entries of ``git status --porcelain=v2 -z``: each ``S...`` entry, and each entry whose ``HEAD`` or
    index mode is a gitlink's, so a deleted or renamed submodule counts too. ``record`` paths are left out: the publish
    records them. Also returns the paths of the submodules whose files changed."""
    tokens = output.split("\0")
    changes: list[KeptOutSubmodule] = []
    with_files: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.startswith("1 "):
            _, xy, sub, mode_head, mode_index, mode_tree, _, _, path = token.split(" ", 8)
            orig = None
        elif token.startswith("2 "):
            _, xy, sub, mode_head, mode_index, mode_tree, _, _, _, path = token.split(" ", 9)
            orig = tokens[index]
            index += 1
        elif token.startswith("u "):
            fields = token.split(" ", 10)
            xy, sub, mode_tree, path, orig = fields[1], fields[2], fields[6], fields[10], None
            mode_head = mode_index = fields[3]
        else:
            continue
        if not (sub.startswith("S") or _GITLINK_MODE in (mode_head, mode_index)):
            continue
        if orig is not None:
            if orig not in record:
                changes.append(KeptOutSubmodule(path=orig, removed=True))
            if path not in record:
                changes.append(KeptOutSubmodule(path=path, added=True))
            continue
        if path in record:
            continue
        removed = "D" in xy or mode_tree == _NO_MODE
        changes.append(
            KeptOutSubmodule(path=path, commit_moved=sub[1:2] == "C", removed=removed, added=mode_head == _NO_MODE)
        )
        if not removed and (sub[2:3] == "M" or sub[3:4] == "U"):
            with_files.add(path)
    return changes, with_files


def _with_changed_files(sub: KeptOutSubmodule, files: Sequence[str]) -> KeptOutSubmodule:
    return replace(sub, changed_files=tuple(files[:KEPT_OUT_FILES_CAP]), more=max(len(files) - KEPT_OUT_FILES_CAP, 0))


def _parse_porcelain_paths(output: str) -> tuple[str, ...]:
    """The paths of ``git status --porcelain -z``, both of a rename's, in order and without repeats."""
    tokens = [token for token in output.split("\0") if token]
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4:
            continue
        paths.append(token[3:])
        if token[0] in "RC":
            if index < len(tokens):
                paths.append(tokens[index])
            index += 1
    return tuple(dict.fromkeys(paths))


def _parse_index_gitlinks(output: str) -> dict[str, str]:
    """``path -> commit`` of the gitlinks ``git ls-files -s -z`` lists."""
    gitlinks: dict[str, str] = {}
    for entry in output.split("\0"):
        meta, tab, path = entry.partition("\t")
        parts = meta.split()
        if tab and len(parts) == 3 and parts[0] == _GITLINK_MODE:
            gitlinks[path] = parts[1]
    return gitlinks


def _parse_raw_gitlinks(output: str) -> list[str]:
    """The paths ``git diff --cached --raw -z --no-renames`` shows with a gitlink on either side."""
    tokens = output.split("\0")
    paths: list[str] = []
    for meta, path in zip(tokens[0::2], tokens[1::2], strict=False):
        parts = meta.lstrip(":").split()
        if len(parts) >= 2 and _GITLINK_MODE in (parts[0], parts[1]):
            paths.append(path)
    return paths


def _parse_unmerged(output: str) -> dict[str, dict[str, str]]:
    """``path -> {stage: sha}`` of ``git ls-files -u -z``."""
    unmerged: dict[str, dict[str, str]] = {}
    for entry in output.split("\0"):
        meta, tab, path = entry.partition("\t")
        parts = meta.split()
        if tab and len(parts) == 3:
            unmerged.setdefault(path, {})[parts[2]] = parts[1]
    return unmerged


def _submodule_summary(recorded_from: Mapping[str, str], record: Mapping[str, str], logs: Sequence[GitResult]) -> str:
    """``git diff --submodule=log``'s lines for each recorded commit, or git's "commits not present" line when the
    submodule's clone lacks them (a pin, in a sandbox that never fetched the merged commit)."""
    lines: list[str] = []
    for (path, old), log in zip(recorded_from.items(), logs, strict=True):
        new = record[path]
        if log.exit_code == 0:
            lines.append(f"Submodule {path} {old[:7]}..{new[:7]}:")
            lines += [line for line in log.output.splitlines() if line.startswith("  > ")]
        else:
            lines.append(f"Submodule {path} {old[:7]}...{new[:7]} (commits not present)")
    return "\n".join(lines) + "\n"
