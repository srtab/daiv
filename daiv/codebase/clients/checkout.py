"""Completing a clone: its submodules and its Git LFS content, as a repository's ``.daiv.yml`` ``checkout`` asks.

``RepoClient.load_repo`` clones the superproject alone. ``RepoCheckout`` then checks out each submodule on the run's
git platform that DAIV works on and the run's person can read, one at a time and recursively, each with a credential
minted for its repository alone, and fetches LFS content from the platform's own LFS endpoint, up to the operator's
cap. What it fetched and left out, and why, comes back as a ``CheckoutReport``. Nothing here fails the run: a
submodule or an LFS file that can't be fetched is reported, and the rest goes on.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess  # noqa: S404
import time
from collections.abc import Mapping  # noqa: TC003
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar, Protocol
from urllib.parse import urlparse, urlunparse

from git import GitCommandError

from codebase.base import Repository  # noqa: TC001
from codebase.clients.base import GIT_INHERITED_ENV, GIT_NO_PROMPT_ENV
from codebase.utils import is_git_object_missing_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from git import Repo

    from codebase.clients.base import GitAuthEnv, RepoClient
    from codebase.repo_config import Checkout

logger = logging.getLogger("daiv.clients")

LFS_FILTER_CONFIG: Mapping[str, str] = MappingProxyType({
    "filter.lfs.process": "git-lfs filter-process",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.required": "true",
})
"""The git config that runs git-lfs as the LFS filter, passed per command: the worker's git config is left alone."""

LFS_ARGV_LIMIT = 64 * 1024
_LFS_POINTER_MAX_BYTES = 1024
_LFS_PATTERN_SPECIALS = re.compile(r"([*?\[\]\\])")
_LFS_SPEC_LINE = "version https://git-lfs.github.com/spec/v1"
_GITLINK_MODE = "160000"
_FETCHED_MTIME_LAG_SECONDS = 2
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)

_EMPTY_MAPPING: Mapping = MappingProxyType({})


@dataclass(frozen=True)
class CheckoutOptions:
    """What one clone completes.

    ``branches`` maps a submodule's path to the working branch an earlier turn published it on (research R19), and
    ``pins`` maps it to ``(recorded_sha, merged_sha)`` for a ``submodule_merged`` run (R21).
    """

    submodules: bool = False
    lfs: bool = False
    lfs_max_bytes: int = 0
    branches: Mapping[str, str] = field(default=_EMPTY_MAPPING)
    pins: Mapping[str, tuple[str, str]] = field(default=_EMPTY_MAPPING)

    @classmethod
    def off(cls) -> CheckoutOptions:
        return cls()

    @classmethod
    def from_config(
        cls,
        checkout: Checkout,
        lfs_max_bytes: int,
        *,
        branches: Mapping[str, str] | None = None,
        pins: Mapping[str, tuple[str, str]] | None = None,
    ) -> CheckoutOptions:
        return cls(
            submodules=checkout.submodules,
            lfs=checkout.lfs,
            lfs_max_bytes=lfs_max_bytes,
            branches=MappingProxyType(dict(branches or {})),
            pins=MappingProxyType(dict(pins or {})),
        )

    @property
    def is_off(self) -> bool:
        return not (self.submodules or self.lfs)


class RunRepoAccess(Protocol):
    """What the run's person may do with a repository, as DAIV's repository access records it, and whether DAIV works
    on it."""

    def can_read(self, repo_id: str) -> bool: ...

    def can_write(self, repo_id: str) -> bool: ...

    def in_catalog(self, repo_id: str) -> bool: ...


class _DenyAll:
    def can_read(self, repo_id: str) -> bool:
        return False

    def can_write(self, repo_id: str) -> bool:
        return False

    def in_catalog(self, repo_id: str) -> bool:
        return False


DENY_ALL: RunRepoAccess = _DenyAll()


class SkipReason(StrEnum):
    """Why a submodule or an LFS file was not fetched."""

    OUTSIDE_PLATFORM = "outside_platform"
    NOT_CATALOGUED = "not_catalogued"
    DAIV_NO_ACCESS = "daiv_no_access"
    PERSON_NO_ACCESS = "person_no_access"
    COMMIT_MISSING = "commit_missing"
    OVER_LIMIT = "over_limit"
    FETCH_FAILED = "fetch_failed"


class PublishBlock(StrEnum):
    """Why DAIV does not publish the changes inside a checked-out submodule."""

    PUBLISHED_ELSEWHERE = "published_elsewhere"
    PARENT_NOT_PUBLISHED = "parent_not_published"
    PERSON_NO_WRITE = "person_no_write"


class SubmoduleNote(StrEnum):
    """What the agent should know about a submodule's checkout beyond its verdict."""

    BRANCH_MOVED_ON = "branch_moved_on"
    """Its working branch no longer contains the commit its parent records, so changes open a new merge request."""
    PIN_LEFT = "pin_left"
    """Its parent no longer records the commit DAIV pushed, so the merged commit is not recorded."""


@dataclass(frozen=True)
class SubmoduleCheckout:
    """One submodule of a clone; ``path`` and ``parent_path`` are relative to the top of the clone."""

    path: str
    parent_path: str
    url: str
    repo_id: str | None
    repository: Repository | None = field(default=None, compare=False, repr=False)
    commit: str = ""
    skipped: SkipReason | None = None
    publish_block: PublishBlock | None = None
    target_branch: str = ""
    branch: str = ""
    pinned: str = ""
    note: SubmoduleNote | str = ""


@dataclass(frozen=True)
class LfsFile:
    path: str
    size: int
    skipped: SkipReason | None = None


@dataclass(frozen=True)
class CheckoutReport:
    """What one clone fetched and left out. Empty (both settings off) for every repository that didn't opt in."""

    EMPTY: ClassVar[CheckoutReport]

    options: CheckoutOptions
    submodules: tuple[SubmoduleCheckout, ...] = ()
    lfs_files: tuple[LfsFile, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.options.is_off

    @property
    def checked_out_submodules(self) -> tuple[SubmoduleCheckout, ...]:
        return tuple(sub for sub in self.submodules if sub.skipped is None)

    @property
    def skipped_submodules(self) -> tuple[SubmoduleCheckout, ...]:
        return tuple(sub for sub in self.submodules if sub.skipped is not None)

    @property
    def skipped_lfs_files(self) -> tuple[LfsFile, ...]:
        return tuple(file for file in self.lfs_files if file.skipped is not None)


CheckoutReport.EMPTY = CheckoutReport(CheckoutOptions.off())


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True)
class _Declared:
    """A submodule as ``.gitmodules`` declares it."""

    name: str
    url: str
    branch: str


@dataclass(frozen=True)
class _LfsCandidate:
    repo_dir: Path
    repository: Repository
    scoped: bool
    path: str
    """Relative to ``repo_dir``."""
    report_path: str
    """Relative to the top of the clone."""
    pointer: bytes
    size: int


def strip_userinfo(url: str) -> str:
    """``url`` without the user info it may carry: ``https://user:token@host/x`` and ``git@host:x`` lose the part
    before ``@``."""
    if "://" in url:
        parsed = urlparse(url)
        if "@" in parsed.netloc:
            return urlunparse(parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[1]))
        return url
    head, sep, tail = url.partition(":")
    if sep and "@" in head and "/" not in head:
        return f"{head.rsplit('@', 1)[1]}:{tail}"
    return url


def _scrub(text: str) -> str:
    return _URL_USERINFO.sub(r"\1", text)


def _run_command(argv: Sequence[str], cwd: Path, env: Mapping[str, str], stdin: bytes | None) -> CommandResult:
    proc = subprocess.run(  # noqa: S603
        list(argv), cwd=cwd, env=dict(env), input=stdin, capture_output=True, check=False
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr.decode(errors="replace"))


class RepoCheckout:
    """Completes one clone: checks out its submodules and fetches its LFS content, as ``options`` asks.

    Every git command runs with hooks off and with only the variables of the worker's environment that git needs, plus
    the one credential that command needs. A submodule's commands get its own repository's credential, minted through
    ``client.with_git_auth(..., scoped_only=True)``, and only the protocol of the platform's clone URL
    (``GIT_ALLOW_PROTOCOL``). LFS content comes from ``<clone_url>/info/lfs`` alone, whatever ``.lfsconfig`` says.

    Args:
        client: The run's platform client: repository lookups and credentials.
        options: What to complete.
        access: What the run's person may read and write, and which repositories DAIV works on.
        allowed_protocol: The only protocol a submodule fetch may use; tests allow ``file``.
        lfs_filter_config: The ``filter.lfs.*`` config that runs git-lfs; tests pass a stand-in.
    """

    run_command: Callable[[Sequence[str], Path, Mapping[str, str], bytes | None], CommandResult] = staticmethod(
        _run_command
    )

    def __init__(
        self,
        client: RepoClient,
        *,
        options: CheckoutOptions,
        access: RunRepoAccess,
        allowed_protocol: str = "https",
        lfs_filter_config: Mapping[str, str] = LFS_FILTER_CONFIG,
    ) -> None:
        self.client = client
        self.options = options
        self.access = access
        self.allowed_protocol = allowed_protocol
        self.lfs_filter_config = lfs_filter_config

    def complete(self, repo: Repo, repository: Repository, *, parent_base_branch: str = "") -> CheckoutReport:
        """Complete ``repo``, the clone of ``repository``; never raises. ``parent_base_branch`` is the branch the run
        bases the repository's merge request on, which a ``.gitmodules`` ``branch = .`` names."""
        if self.options.is_off:
            return CheckoutReport.EMPTY
        top = Path(repo.working_dir)
        submodules: list[SubmoduleCheckout] = []
        lfs_files: list[LfsFile] = []
        try:
            if self.options.submodules:
                _SubmoduleWalk(self, top, repository).run(parent_base_branch, submodules)
            if self.options.lfs:
                self._fetch_lfs(top, repository, submodules, lfs_files)
        except Exception:
            # The clone itself is intact: the run goes on with what was fetched, and the report says the rest.
            logger.exception("Completing the checkout of %s failed part-way", repository.slug)
        return CheckoutReport(options=self.options, submodules=tuple(submodules), lfs_files=tuple(lfs_files))

    # -- commands --------------------------------------------------------------
    def git(
        self,
        cwd: Path,
        *args: str,
        auth_env: GitAuthEnv | None = None,
        config: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> CommandResult:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
        return self._run(argv, cwd, self.env(auth_env, config), stdin, check=check)

    def env(
        self, auth_env: GitAuthEnv | None, config: Mapping[str, str] | None = None, *, skip_smudge: bool = True
    ) -> dict[str, str]:
        """A command's environment: the inherited variables, no prompts, one protocol, the credential, and ``config``
        as command-scoped git config."""
        env = {name: os.environ[name] for name in GIT_INHERITED_ENV if name in os.environ}
        env |= GIT_NO_PROMPT_ENV
        env["GIT_ALLOW_PROTOCOL"] = self.allowed_protocol
        if skip_smudge:
            env["GIT_LFS_SKIP_SMUDGE"] = "1"
        pairs: list[tuple[str, str]] = []
        if auth_env is not None:
            auth = auth_env.as_env()
            pairs.append((auth["GIT_CONFIG_KEY_0"], auth["GIT_CONFIG_VALUE_0"]))
        pairs += list((config or {}).items())
        if pairs:
            env["GIT_CONFIG_COUNT"] = str(len(pairs))
            for index, (key, value) in enumerate(pairs):
                env[f"GIT_CONFIG_KEY_{index}"] = key
                env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    def _run(
        self, argv: list[str], cwd: Path, env: Mapping[str, str], stdin: bytes | None, *, check: bool
    ) -> CommandResult:
        result = self.run_command(argv, cwd, env, stdin)
        if check and result.exit_code != 0:
            raise GitCommandError(argv, result.exit_code, stderr=_scrub(result.stderr))
        return result

    # -- LFS ---------------------------------------------------------------------
    def _fetch_lfs(
        self, top: Path, repository: Repository, submodules: Sequence[SubmoduleCheckout], out: list[LfsFile]
    ) -> None:
        """List the LFS pointers of the superproject and of every checked-out submodule, take them in path order,
        superproject first, while they fit under the cap, fetch those, and make git see the fetched files as
        unchanged."""
        repos: list[tuple[Path, str, Repository, bool]] = [(top, "", repository, False)]
        repos += [
            (top / sub.path, f"{sub.path}/", sub.repository, True)
            for sub in submodules
            if sub.skipped is None and sub.repository is not None
        ]
        candidates: list[_LfsCandidate] = []
        for repo_dir, prefix, repo_repository, scoped in repos:
            try:
                pointers = self._lfs_pointers(repo_dir)
            except Exception:
                logger.exception("Could not list the LFS files of %s", repo_repository.slug)
                continue
            candidates += [
                _LfsCandidate(repo_dir, repo_repository, scoped, path, f"{prefix}{path}", pointer, size)
                for path, pointer, size in pointers
            ]

        remaining = self.options.lfs_max_bytes
        selected: list[_LfsCandidate] = []
        skipped: dict[str, SkipReason] = {}
        for candidate in candidates:
            if candidate.size <= remaining:
                selected.append(candidate)
                remaining -= candidate.size
            else:
                skipped[candidate.report_path] = SkipReason.OVER_LIMIT
        if skipped:
            logger.info("Left %d LFS file(s) of %s over the download limit", len(skipped), repository.slug)

        failed = self._pull_lfs(selected)
        fetched: dict[Path, list[_LfsCandidate]] = {}
        for candidate in selected:
            if candidate.report_path in failed:
                skipped[candidate.report_path] = SkipReason.FETCH_FAILED
            else:
                fetched.setdefault(candidate.repo_dir, []).append(candidate)
        for repo_dir, files in fetched.items():
            self._settle_stat(repo_dir, files)
        out.extend(
            LfsFile(path=candidate.report_path, size=candidate.size, skipped=skipped.get(candidate.report_path))
            for candidate in candidates
        )

    def _lfs_pointers(self, repo_dir: Path) -> list[tuple[str, bytes, int]]:
        """``(path, pointer, size)`` of each LFS pointer the repository's index holds, in path order. A path whose
        ``filter`` attribute is ``lfs`` but whose blob is no pointer was committed without LFS, and is left out."""
        entries = self._index_blobs(repo_dir)
        if not entries:
            return []
        attrs = self.git(
            repo_dir, "check-attr", "-z", "--stdin", "filter", stdin="\0".join(entries).encode() + b"\0"
        ).stdout.split(b"\0")
        lfs_paths = [
            attrs[i].decode()
            for i in range(0, len(attrs) - 2, 3)
            if attrs[i + 1] == b"filter" and attrs[i + 2] == b"lfs"
        ]
        if not lfs_paths:
            return []
        shas = [entries[path] for path in lfs_paths]
        checks = self.git(repo_dir, "cat-file", "--batch-check", stdin="".join(f"{sha}\n" for sha in shas).encode())
        small = [
            (path, sha)
            for path, sha, line in zip(lfs_paths, shas, checks.stdout.decode().splitlines(), strict=False)
            if (parts := line.split())
            and len(parts) == 3
            and parts[1] == "blob"
            and int(parts[2]) <= _LFS_POINTER_MAX_BYTES
        ]
        if not small:
            return []
        blobs = self._read_blobs(repo_dir, [sha for _, sha in small])
        pointers = []
        for (path, sha), blob in zip(small, blobs, strict=True):
            if (size := _pointer_size(blob)) is not None:
                pointers.append((path, blob, size))
            else:
                logger.debug("%s in %s has the LFS filter but holds no pointer (sha %s)", path, repo_dir, sha)
        return sorted(pointers)

    def _index_blobs(self, repo_dir: Path) -> dict[str, str]:
        """Each regular file the index holds, mapped to its blob's sha."""
        output = self.git(repo_dir, "ls-files", "-s", "-z").stdout.decode(errors="surrogateescape")
        blobs: dict[str, str] = {}
        for entry in output.split("\0"):
            meta, tab, path = entry.partition("\t")
            parts = meta.split()
            if tab and len(parts) == 3 and parts[0] != _GITLINK_MODE and parts[2] == "0":
                blobs[path] = parts[1]
        return blobs

    def _read_blobs(self, repo_dir: Path, shas: Sequence[str]) -> list[bytes]:
        output = self.git(repo_dir, "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha in shas).encode()).stdout
        blobs: list[bytes] = []
        offset = 0
        for _ in shas:
            header_end = output.index(b"\n", offset)
            size = int(output[offset:header_end].split()[2])
            blobs.append(output[header_end + 1 : header_end + 1 + size])
            offset = header_end + 1 + size + 1
        return blobs

    def _pull_lfs(self, selected: Sequence[_LfsCandidate]) -> set[str]:
        """Fetch ``selected``, one repository at a time and in batches whose argv stays under ``LFS_ARGV_LIMIT``; a
        path holding git-lfs's list separator (a comma) is fetched alone with ``git-lfs smudge``. Returns the report
        paths that failed."""
        failed: set[str] = set()
        by_repo: dict[Path, list[_LfsCandidate]] = {}
        for candidate in selected:
            by_repo.setdefault(candidate.repo_dir, []).append(candidate)
        missing_program = False
        for repo_dir, files in by_repo.items():
            if missing_program:
                failed.update(file.report_path for file in files)
                continue
            repository = files[0].repository
            batches = _include_batches([file for file in files if "," not in file.path])
            singles = [[file] for file in files if "," in file.path]
            for batch in [*batches, *singles]:
                try:
                    self.client.with_git_auth(
                        repository, partial(self._lfs_batch, repo_dir, repository, batch), scoped_only=files[0].scoped
                    )
                except FileNotFoundError:
                    logger.error(  # noqa: TRY400
                        "The git-lfs program is not installed on the worker, so no LFS content was downloaded; "
                        "install the git-lfs package in DAIV's image"
                    )
                    missing_program = True
                    failed.update(file.report_path for file in files)
                    break
                except Exception as exc:
                    logger.error(  # noqa: TRY400
                        "Downloading %d LFS file(s) of %s failed (%s): %s",
                        len(batch),
                        repository.slug,
                        type(exc).__name__,
                        _scrub(getattr(exc, "stderr", "") or str(exc)).strip(),
                    )
                    failed.update(file.report_path for file in batch)
        return failed

    def _lfs_batch(
        self, repo_dir: Path, repository: Repository, batch: Sequence[_LfsCandidate], auth_env: GitAuthEnv | None
    ) -> None:
        config = {"lfs.url": f"{repository.clone_url}/info/lfs", **self.lfs_filter_config}
        env = self.env(auth_env, config, skip_smudge=False)
        if len(batch) == 1 and "," in batch[0].path:
            (file,) = batch
            content = self._run(["git-lfs", "smudge", "--", file.path], repo_dir, env, file.pointer, check=True).stdout
            (repo_dir / file.path).write_bytes(content)
            return
        include = ",".join(_LFS_PATTERN_SPECIALS.sub(r"\\\1", file.path) for file in batch)
        self._run(["git-lfs", "pull", f"--include={include}", "--exclude="], repo_dir, env, None, check=True)

    def _settle_stat(self, repo_dir: Path, files: Sequence[_LfsCandidate]) -> None:
        """Make git see the fetched files as unchanged without an LFS filter, as a sandbox's git has none: git compares
        only whole-second mtime and size, every fetched file is back-dated, and the files are re-staged through the
        LFS filter on the worker, which yields their pointers again and records their stat data in an index newer than
        every one of them.

        Re-staging, not ``update-index --refresh``: a refresh trusts a changed size to mean changed content and never
        reads the file, and a fetched file's size is its content's, not its pointer's.
        """
        slug = files[0].repository.slug
        try:
            self.git(repo_dir, "config", "core.checkStat", "minimal")
            self.git(repo_dir, "config", "core.trustctime", "false")
            stamp = time.time() - _FETCHED_MTIME_LAG_SECONDS
            for file in files:
                os.utime(repo_dir / file.path, (stamp, stamp))
            self.git(
                repo_dir,
                "--literal-pathspecs",
                "add",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                config=self.lfs_filter_config,
                stdin=b"".join(file.path.encode() + b"\0" for file in files),
            )
            if self.git(repo_dir, "diff", "--cached", "--quiet", check=False).exit_code != 0:
                logger.error(
                    "The LFS filter changed the pointers of %s's fetched files; leaving its index as cloned", slug
                )
                self.git(repo_dir, "reset", "-q")
        except Exception:
            logger.exception("Could not settle git's view of the LFS files of %s; git may show them as changed", slug)


class _SubmoduleWalk:
    """Checks out one clone's submodules, depth first, each after its own checks (research R2–R4, R14, R17, R19,
    R21)."""

    def __init__(self, checkout: RepoCheckout, top: Path, repository: Repository) -> None:
        self.checkout = checkout
        self.top = top
        self.repository = repository
        parsed = urlparse(repository.clone_url)
        self.scheme = parsed.scheme
        self.netloc = parsed.netloc.rsplit("@", 1)[-1]
        self.host = (parsed.hostname or "").lower()
        suffix = f"{repository.slug}.git"
        self.path_prefix = parsed.path[: -len(suffix)] if parsed.path.endswith(suffix) else "/"

    def run(self, base_branch: str, out: list[SubmoduleCheckout]) -> None:
        self._walk(
            self.top, prefix="", parent_url=self._platform_url(self.repository.slug), base_branch=base_branch, out=out
        )

    def _walk(
        self, parent_dir: Path, *, prefix: str, parent_url: str, base_branch: str, out: list[SubmoduleCheckout]
    ) -> None:
        declared = self._declared(parent_dir)
        for path, commit in self._gitlinks(parent_dir):
            if (decl := declared.get(path)) is None:
                logger.info("Submodule path %s%s has no .gitmodules entry; not checking it out", prefix, path)
                continue
            try:
                sub, resolved_url = self._one(parent_dir, decl, path, prefix, commit, parent_url, base_branch)
            except Exception:
                logger.exception("Checking out the submodule %s%s of %s failed", prefix, path, self.repository.slug)
                sub, resolved_url = (
                    SubmoduleCheckout(
                        path=f"{prefix}{path}",
                        parent_path=prefix.rstrip("/"),
                        url=strip_userinfo(decl.url),
                        repo_id=None,
                        commit=commit,
                        skipped=SkipReason.FETCH_FAILED,
                    ),
                    "",
                )
            out.append(sub)
            if sub.skipped is None:
                self._walk(
                    parent_dir / path,
                    prefix=f"{sub.path}/",
                    parent_url=resolved_url,
                    base_branch=sub.target_branch,
                    out=out,
                )

    def _one(
        self, parent_dir: Path, decl: _Declared, path: str, prefix: str, commit: str, parent_url: str, base_branch: str
    ) -> tuple[SubmoduleCheckout, str]:
        full_path = f"{prefix}{path}"
        base = SubmoduleCheckout(
            path=full_path, parent_path=prefix.rstrip("/"), url=strip_userinfo(decl.url), repo_id=None, commit=commit
        )
        resolved = self.resolve(decl.url, parent_url)
        if resolved is None:
            logger.info(
                "Not checking out the submodule %s of %s: it is outside the platform", full_path, self.repository.slug
            )
            return replace(base, skipped=SkipReason.OUTSIDE_PLATFORM), ""
        repo_id, resolved_url = resolved
        base = replace(base, repo_id=repo_id)
        access = self.checkout.access
        if not access.in_catalog(repo_id):
            logger.info(
                "Not checking out the submodule %s (%s): DAIV doesn't work on that repository", full_path, repo_id
            )
            return replace(base, skipped=SkipReason.NOT_CATALOGUED), resolved_url
        try:
            sub_repository = self.checkout.client.get_repository(repo_id)
        except Exception:
            logger.exception(
                "Not checking out the submodule %s of %s: DAIV's account can't read %s",
                full_path,
                self.repository.slug,
                repo_id,
            )
            return replace(base, skipped=SkipReason.DAIV_NO_ACCESS), resolved_url
        base = replace(base, repository=sub_repository)
        if not access.can_read(repo_id):
            logger.info("Not checking out the submodule %s (%s): the run's person can't read it", full_path, repo_id)
            return replace(base, skipped=SkipReason.PERSON_NO_ACCESS), resolved_url

        try:
            self.checkout.client.with_git_auth(
                sub_repository, partial(self._update, parent_dir, decl, path, sub_repository), scoped_only=True
            )
        except GitCommandError as exc:
            text = f"{exc.stderr or ''} {exc.stdout or ''}"
            if is_git_object_missing_text(text):
                logger.info(
                    "Not checking out the submodule %s (%s): the commit %s is missing from it",
                    full_path,
                    repo_id,
                    commit,
                )
                return replace(base, skipped=SkipReason.COMMIT_MISSING), resolved_url
            logger.error(  # noqa: TRY400
                "Fetching the submodule %s (%s) of %s failed: %s",
                full_path,
                repo_id,
                self.repository.slug,
                _scrub(text).strip(),
            )
            self._discard_partial(parent_dir, decl.name, path)
            return replace(base, skipped=SkipReason.FETCH_FAILED), resolved_url
        except Exception:
            logger.exception("Fetching the submodule %s (%s) of %s failed", full_path, repo_id, self.repository.slug)
            self._discard_partial(parent_dir, decl.name, path)
            return replace(base, skipped=SkipReason.FETCH_FAILED), resolved_url

        sub_dir = parent_dir / path
        block = None
        if not access.can_write(repo_id):
            logger.info("The submodule %s (%s) is read-only: the run's person can't write it", full_path, repo_id)
            block = PublishBlock.PERSON_NO_WRITE
        target_branch = self._target_branch(sub_dir, decl.branch, base_branch, sub_repository.default_branch)
        branch, pinned, note = self._hints(sub_dir, full_path, commit)
        return (
            replace(base, publish_block=block, target_branch=target_branch, branch=branch, pinned=pinned, note=note),
            resolved_url,
        )

    def _update(
        self, parent_dir: Path, decl: _Declared, path: str, sub_repository: Repository, auth_env: GitAuthEnv | None
    ) -> None:
        """Clone and check out one submodule at the commit its parent records, with ``auth_env`` alone.
        ``--checkout`` overrides any ``update`` mode the repository sets, and ``--no-recommend-shallow`` any
        ``shallow = true``, which would leave a clone later steps can't diff or branch from."""
        self._discard_partial(parent_dir, decl.name, path)
        git = partial(self.checkout.git, parent_dir, auth_env=auth_env)
        git("submodule", "init", "--", path)
        git("config", f"submodule.{decl.name}.url", sub_repository.clone_url)
        git("submodule", "update", "--checkout", "--no-recommend-shallow", "--", path)

    def _discard_partial(self, parent_dir: Path, name: str, path: str) -> None:
        """Remove what a failed attempt left: the submodule's git directory and its files, keeping the empty
        directory git checked out for it."""
        git_dir = self.checkout.git(parent_dir, "rev-parse", "--absolute-git-dir").stdout.decode().strip()
        modules = Path(git_dir) / "modules" / name
        if modules.exists():
            shutil.rmtree(modules)
        sub_dir = parent_dir / path
        if sub_dir.is_dir():
            for child in sub_dir.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()

    def _target_branch(self, sub_dir: Path, declared: str, base_branch: str, default_branch: str) -> str:
        """The branch ``.gitmodules`` names (``.`` is the parent's base branch) when the submodule's remote has it, else
        the submodule repository's default branch."""
        wanted = base_branch if declared == "." else declared
        if wanted and self._has_remote_branch(sub_dir, wanted):
            return wanted
        return default_branch

    def _has_remote_branch(self, sub_dir: Path, branch: str) -> bool:
        return (
            self.checkout.git(
                sub_dir, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}", check=False
            ).exit_code
            == 0
        )

    def _hints(self, sub_dir: Path, full_path: str, commit: str) -> tuple[str, str, SubmoduleNote | str]:
        """Apply the session's hints for ``full_path``: the merged commit a pin records (R21), and the working branch
        an earlier turn published it on, when that branch still contains ``commit`` (R19). A git failure leaves the
        submodule at ``commit``, detached."""
        options = self.checkout.options
        pinned = ""
        note: SubmoduleNote | str = ""
        if (pin := options.pins.get(full_path)) is not None:
            recorded_sha, merged_sha = pin
            if commit == recorded_sha:
                pinned = merged_sha
            else:
                note = SubmoduleNote.PIN_LEFT
        branch = ""
        if (wanted := options.branches.get(full_path)) and self._has_remote_branch(sub_dir, wanted):
            try:
                contains = self.checkout.git(
                    sub_dir, "merge-base", "--is-ancestor", commit, f"refs/remotes/origin/{wanted}", check=False
                )
                if contains.exit_code == 0:
                    self.checkout.git(sub_dir, "checkout", "-q", "-B", wanted, f"refs/remotes/origin/{wanted}")
                    branch = wanted
                elif contains.exit_code == 1:
                    note = note or SubmoduleNote.BRANCH_MOVED_ON
                else:
                    raise GitCommandError(["git", "merge-base", "--is-ancestor"], contains.exit_code, contains.stderr)
            except Exception:
                logger.exception("Could not check out the working branch %s of the submodule %s", wanted, full_path)
                return "", "", ""
        return branch, pinned, note

    # -- reading the parent ------------------------------------------------------------
    def _declared(self, parent_dir: Path) -> dict[str, _Declared]:
        """The submodules ``.gitmodules`` declares, by path. Read with ``-f``, which follows no ``include``."""
        if not (parent_dir / ".gitmodules").is_file():
            return {}
        result = self.checkout.git(
            parent_dir,
            "config",
            "-f",
            ".gitmodules",
            "-z",
            "--get-regexp",
            r"^submodule\..*\.(path|url|branch)$",
            check=False,
        )
        if result.exit_code not in (0, 1):
            raise GitCommandError(["git", "config", "-f", ".gitmodules"], result.exit_code, result.stderr)
        fields: dict[str, dict[str, str]] = {}
        for entry in result.stdout.decode(errors="surrogateescape").split("\0"):
            key, _, value = entry.partition("\n")
            if not key.startswith("submodule."):
                continue
            name, _, attribute = key.removeprefix("submodule.").rpartition(".")
            fields.setdefault(name, {})[attribute] = value
        return {
            values["path"]: _Declared(name=name, url=values["url"], branch=values.get("branch", ""))
            for name, values in fields.items()
            if values.get("path") and values.get("url")
        }

    def _gitlinks(self, parent_dir: Path) -> list[tuple[str, str]]:
        """``(path, commit)`` of each submodule the parent's index records, in path order."""
        output = self.checkout.git(parent_dir, "ls-files", "-s", "-z").stdout.decode(errors="surrogateescape")
        links = []
        for entry in output.split("\0"):
            meta, tab, path = entry.partition("\t")
            parts = meta.split()
            if tab and len(parts) == 3 and parts[0] == _GITLINK_MODE:
                links.append((path, parts[1]))
        return links

    # -- URLs ------------------------------------------------------------------------
    def resolve(self, url: str, parent_url: str) -> tuple[str, str] | None:
        """The platform repository path ``url`` names and its HTTPS URL, or ``None`` when it is outside the platform:
        another host, or a scheme other than HTTP(S) and SSH. A relative URL resolves against ``parent_url``."""
        if url.startswith(("./", "../")):
            if not parent_url:
                return None
            return self._from_platform_url(_join_relative(parent_url, url))
        if "::" in url:
            return None
        if "://" in url:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
            if (parsed.hostname or "").lower() != self.host:
                return None
            if scheme in ("http", "https"):
                return self._from_platform_url(url)
            if scheme in ("ssh", "git+ssh", "ssh+git"):
                return self._repo(parsed.path)
            return None
        head, sep, tail = url.partition(":")
        if sep and "/" not in head and head:
            host = head.rsplit("@", 1)[-1].lower()
            return self._repo(tail) if host == self.host else None
        return None

    def _from_platform_url(self, url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != self.host or not parsed.path.startswith(self.path_prefix):
            return None
        return self._repo(parsed.path[len(self.path_prefix) :])

    def _repo(self, path: str) -> tuple[str, str] | None:
        repo_id = path.strip("/").removesuffix(".git").strip("/")
        if not repo_id or ".." in repo_id.split("/"):
            return None
        return repo_id, self._platform_url(repo_id)

    def _platform_url(self, repo_id: str) -> str:
        return f"{self.scheme}://{self.netloc}{self.path_prefix}{repo_id}.git"


def _join_relative(base_url: str, relative: str) -> str:
    """``relative`` resolved against ``base_url`` as git resolves a submodule's relative URL: each ``../`` drops one
    path component of the base, ``./`` none."""
    parsed = urlparse(base_url)
    parts = [part for part in parsed.path.rstrip("/").split("/") if part]
    rest = relative
    while rest.startswith(("./", "../")):
        if rest.startswith("../"):
            if parts:
                parts.pop()
            rest = rest[3:]
        else:
            rest = rest[2:]
    return urlunparse(parsed._replace(path="/" + "/".join([*parts, rest.strip("/")])))


def _pointer_size(blob: bytes) -> int | None:
    """The object size an LFS pointer declares, or ``None`` when ``blob`` is no pointer."""
    try:
        lines = blob.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return None
    if not lines or lines[0] != _LFS_SPEC_LINE:
        return None
    fields = dict(line.split(" ", 1) for line in lines[1:] if " " in line)
    oid, size = fields.get("oid", ""), fields.get("size", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", oid) or not size.isdigit():
        return None
    return int(size)


def _include_batches(files: Iterable[_LfsCandidate]) -> list[list[_LfsCandidate]]:
    """``files`` split so each ``--include`` argument stays under ``LFS_ARGV_LIMIT``."""
    batches: list[list[_LfsCandidate]] = []
    current: list[_LfsCandidate] = []
    length = 0
    for file in files:
        cost = len(_LFS_PATTERN_SPECIALS.sub(r"\\\1", file.path).encode()) + 1
        if current and length + cost > LFS_ARGV_LIMIT:
            batches.append(current)
            current, length = [], 0
        current.append(file)
        length += cost
    if current:
        batches.append(current)
    return batches
