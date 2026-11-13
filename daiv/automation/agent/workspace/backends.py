"""The ``/workspace`` file backends: deepagents' disk backend with DAIV's regex grep, the disk run's composite and the
fence that keeps its file tools inside it."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

import wcmatch.glob as wcglob
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import DEFAULT_GREP_TIMEOUT, FilesystemBackend
from deepagents.backends.protocol import DeleteResult, EditResult, GrepMatch, GrepResult, WriteResult
from skills.constants import SKILLS_CACHE_PATH, SKILLS_PATH

from automation.agent.constants import EXTRA_REPOS_PATH, REPO_PATH, TMP_PATH, WORKSPACE_PATH

# Through DAIV's middleware module, whose import makes the permission globs below match dot paths.
from automation.agent.middlewares.file_system import FilesystemPermission

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from deepagents.backends.protocol import BackendProtocol

logger = logging.getLogger("daiv.tools")


def _tree(root: str) -> list[str]:
    return [root, f"{root}/**"]


# Offloaded-artifact dirs derived from ``artifacts_root`` (= /workspace in the disk composite).
# deepagents' large-tool-result / conversation-history eviction and git_platform's ``output_to_file``
# all WRITE here through the backend directly (bypassing the fence) and then hand the agent the path
# to read back. Without an explicit read carve-out ahead of the deny, that read-back hits the
# ``/workspace/**`` deny and dead-ends — the full content is written but unrecoverable. Write stays
# denied (the agent never writes here itself; only the framework does, and that bypasses the fence).
# These suffixes mirror deepagents' ``FilesystemMiddleware``; a drift-guard test pins them to the
# framework's computed prefixes so a rename fails loudly instead of silently re-breaking read-back.
WORKSPACE_ARTIFACT_SUBTREES = [
    *_tree(f"{WORKSPACE_PATH}/large_tool_results"),
    *_tree(f"{WORKSPACE_PATH}/conversation_history"),
]

# Disk-mode fence for the file tools: the first matching rule wins and an unmatched path is allowed. Sandbox runs have
# none, since bash is unconstrained there.
WORKSPACE_FENCE_PERMISSIONS = [
    FilesystemPermission(
        operations=["read", "write"],
        paths=[*_tree(f"{REPO_PATH}/**/.git"), *_tree(f"{EXTRA_REPOS_PATH}/**/.git")],
        mode="deny",
    ),
    FilesystemPermission(
        operations=["read", "write"],
        paths=[*_tree(REPO_PATH), *_tree(EXTRA_REPOS_PATH), *_tree(TMP_PATH)],
        mode="allow",
    ),
    FilesystemPermission(operations=["read"], paths=[*_tree(SKILLS_PATH), *WORKSPACE_ARTIFACT_SUBTREES], mode="allow"),
    FilesystemPermission(operations=["read", "write"], paths=_tree(WORKSPACE_PATH), mode="deny"),
]


class DAIVFilesystemBackend(FilesystemBackend):
    """``FilesystemBackend`` with a regex ``agrep``."""

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Regex grep (convergence with the sandbox's ERE search and Claude Code).

        Validates the pattern with Python ``re`` up front so an invalid regex is a clean,
        model-fixable error rather than a silent zero-match (the inherited backend greps literally
        via ``rg -F``/``re.escape``; ripgrep also exits 2 quietly on a bad regex). Walks the tree
        with a compiled regex — trading ripgrep's speed for correct semantics on local/disk runs
        (the deployed path is the sandbox backend).

        ``max_count`` stops the walk once the cap is reached and reports ``truncated``, so the
        cap bounds the work rather than only trimming the result. ``context_lines`` is accepted
        for signature parity with the base but not emitted — the filesystem tools never request
        it (only direct backend callers can, and DAIV has none).
        """
        try:
            re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"invalid regular expression: {pattern!r} ({exc})")
        return await asyncio.to_thread(self._regex_grep, pattern, path, glob, max_count)

    def _grep_error_detail(self, exc: Exception) -> str:
        """Agent-safe detail for a grep failure that never embeds the real on-disk path.

        ``OSError.__str__`` appends the offending filename and, in ``virtual_mode``, even
        generic exception text can carry the backend's real ``root_dir`` — either would leak
        the host layout to the model. Mirrors the sanitisation deepagents applies in
        ``FilesystemBackend._python_search`` (a method-local ``_safe_detail`` we cannot reuse),
        keeping this shadowing regex walk in parity with the base's literal one.
        """
        if isinstance(exc, OSError):
            detail = exc.strerror
        else:
            detail = getattr(exc, "reason", None)
            if detail is None and not self.virtual_mode:
                detail = str(exc)
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

    def _regex_grep(self, pattern: str, path: str | None, glob: str | None, max_count: int | None = None) -> GrepResult:
        try:
            base_full = self._resolve_path(path or ".")
        except ValueError:
            return GrepResult(matches=[])
        except (OSError, RuntimeError) as exc:
            return GrepResult(error=f"Error searching path '{path or '.'}': {self._grep_error_detail(exc)}", matches=[])
        try:
            if not base_full.exists():
                return GrepResult(matches=[])
        except OSError as exc:
            return GrepResult(error=f"Error searching path '{path or '.'}': {self._grep_error_detail(exc)}", matches=[])

        regex = re.compile(pattern)
        glob_matcher = wcglob.compile(glob, flags=wcglob.BRACE | wcglob.GLOBSTAR) if glob else None
        deadline = time.monotonic() + DEFAULT_GREP_TIMEOUT
        root = base_full if base_full.is_dir() else base_full.parent
        results: dict[str, list[tuple[int, str]]] = {}
        file_errors: list[str] = []
        found = 0
        truncated = False

        def _dump_matches() -> list[GrepMatch]:
            return [
                GrepMatch(path=fpath, line=int(line_num), text=line_text)
                for fpath, items in results.items()
                for (line_num, line_text) in items
            ]

        def _timed_out() -> GrepResult:
            return GrepResult(
                error=(
                    f"Grep of '{path or '.'}' timed out after {DEFAULT_GREP_TIMEOUT}s "
                    f"with {len(results)} matching file(s); try a more "
                    f"specific pattern or a narrower path."
                ),
                matches=_dump_matches(),
            )

        try:
            for fp in root.rglob("*"):
                if time.monotonic() > deadline:
                    return _timed_out()
                rel = fp.relative_to(root)
                if ".git" in rel.parts:
                    continue
                try:
                    if not fp.is_file():
                        continue
                except OSError, RuntimeError:
                    continue
                if glob_matcher is not None and not glob_matcher.match(str(rel)):
                    continue
                try:
                    if fp.stat().st_size > self.max_file_size_bytes:
                        continue
                except OSError, RuntimeError:
                    continue
                scanned_lines = 0
                try:
                    if self.virtual_mode:
                        try:
                            virt_path = self._to_virtual_path(fp)
                        except ValueError:
                            # Resolved outside the virtual root — expected for stray symlinks; the
                            # base logs this at DEBUG, so mirror it rather than dropping silently.
                            logger.debug("skipping grep result outside root: %s", fp)
                            continue
                        except OSError, RuntimeError:
                            # ``resolve()`` failed (permission denied, or a symlink loop -> ELOOP).
                            # A matched file would be dropped, so log loudly (base parity) instead
                            # of vanishing without a trace.
                            logger.warning("could not resolve grep result path: %s", fp, exc_info=True)
                            continue
                    else:
                        virt_path = str(fp)
                    with fp.open(encoding="utf-8", errors="strict") as handle:
                        for line_num, raw_line in enumerate(handle, 1):
                            scanned_lines = line_num
                            if line_num % 2048 == 0 and time.monotonic() > deadline:
                                return _timed_out()
                            if regex.search(raw_line):
                                results.setdefault(virt_path, []).append((line_num, raw_line.rstrip("\n")))
                                found += 1
                                if max_count is not None and found >= max_count:
                                    truncated = True
                                    break
                except UnicodeDecodeError as exc:
                    # A file that fails to decode before any line is scanned is treated as binary
                    # and skipped silently (mirroring ripgrep). Record it only when the decode
                    # failed partway through (``scanned_lines > 0``), so a truncated per-file read is
                    # logged (and surfaced if nothing else matched) rather than passing as complete.
                    if scanned_lines > 0:
                        file_errors.append(f"- {virt_path}: {self._grep_error_detail(exc)}")
                    continue
                except (OSError, RuntimeError) as exc:
                    file_errors.append(f"- {virt_path}: {self._grep_error_detail(exc)}")
                    continue
                if truncated:
                    break
        except (OSError, RuntimeError) as exc:
            # The tree walk itself aborted mid-iteration (a directory entry unlinked/renamed during
            # the walk, or a symlink loop). Unlike a single unreadable file, the walk is now
            # arbitrarily incomplete, so — deliberately, unlike the per-file path below — we *surface*
            # (the agent must not trust an aborted walk as a complete search) *and* log for operators
            # (base parity: it logs this abort with a traceback).
            logger.warning(
                "disk grep walk of %r aborted after %d matching file(s)", path or ".", len(results), exc_info=True
            )
            return GrepResult(
                error=f"Error searching path '{path or '.'}': {self._grep_error_detail(exc)}", matches=_dump_matches()
            )

        matches = _dump_matches()
        if file_errors:
            # A per-file read failure (permissions, a file unlinked mid-walk, transient I/O) is not
            # agent-actionable. When usable matches survive, return them clean and keep the failures
            # in the operator logs rather than setting ``GrepResult.error`` — ``CompositeBackend``
            # ``.agrep`` treats a set ``error`` as fatal and would drop matches from the *other* routed
            # backends (and the tool marks the call failed). Only when nothing matched do we surface,
            # so an empty-because-unreadable result isn't mistaken for a genuine zero-match. See the
            # partial-result-over-bare-error policy; the base's literal ``_python_search`` always
            # surfaces here — this is a deliberate, documented divergence.
            joined = "\n".join(file_errors)
            logger.warning("disk grep could not fully search %d file(s):\n%s", len(file_errors), joined)
            if not matches:
                return GrepResult(error=f"One or more files could not be fully searched:\n{joined}", matches=matches)
        return GrepResult(matches=matches, truncated=truncated)


class RepoFilesystemBackend(DAIVFilesystemBackend):
    """The clone's backend, refusing every path into a ``.git``: host git runs the hooks and config it finds there.

    The check is on the resolved path, so a symlink committed to the repository cannot alias one.
    """

    def _resolve_path(self, key: str) -> Path:
        resolved = super()._resolve_path(key)
        if ".git" in resolved.relative_to(self.cwd).parts:
            raise PermissionError("git metadata is not accessible")
        return resolved


_SKILLS_READ_ONLY = "the skills directory is read-only"


class SkillsCacheBackend(DAIVFilesystemBackend):
    """The worker-wide skills cache, read by every later run: only ``SkillsMiddleware``'s uploads add to it, so the
    file tools' write, edit and delete are refused."""

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"Error writing file '{file_path}': {_SKILLS_READ_ONLY}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        return EditResult(error=f"Error editing file '{file_path}': {_SKILLS_READ_ONLY}")

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=f"Error deleting '{file_path}': {_SKILLS_READ_ONLY}")


def build_disk_workspace_backend(
    clone_dir: Path, *, skills_cache: Path = SKILLS_CACHE_PATH, extra_clones: Mapping[str, Path] | None = None
) -> CompositeBackend:
    """Build the disk-backed composite that serves the unified ``/workspace`` namespace.

    Routes:
      - ``/workspace/repo/``   → the local git clone (``clone_dir``), with its ``.git`` refused
      - each ``extra_clones`` workspace path → that sibling clone, its ``.git`` refused too
      - ``/workspace/skills/`` → the shared global skills cache (``SKILLS_CACHE_PATH``), read-only to the file tools
      - everything else (the ``/workspace/tmp`` scratchpad and the offloaded artifact dirs derived from
        ``artifacts_root``) → a per-run scratch backend rooted at ``scratch/`` beside the clone, inside the
        ``set_runtime_ctx`` tempdir that is auto-removed at run end. Rooted at the clone's parent instead, a path such
        as ``/repo/.git/config`` would reach the clone past both the route and the fence.

    The skills cache is a route (not copied per run) so the global-cache idempotency in
    ``SkillsMiddleware`` is preserved.
    """
    skills_cache.mkdir(parents=True, exist_ok=True)
    repo_backend = RepoFilesystemBackend(root_dir=clone_dir, virtual_mode=True)
    skills_backend = SkillsCacheBackend(root_dir=skills_cache, virtual_mode=True)
    scratch_backend = DAIVFilesystemBackend(root_dir=clone_dir.parent / "scratch", virtual_mode=True)
    routes: dict[str, BackendProtocol] = {f"{REPO_PATH}/": repo_backend, f"{SKILLS_PATH}/": skills_backend}
    for workspace_path, extra_clone in (extra_clones or {}).items():
        routes[f"{workspace_path}/"] = RepoFilesystemBackend(root_dir=extra_clone, virtual_mode=True)
    return CompositeBackend(default=scratch_backend, routes=routes, artifacts_root=WORKSPACE_PATH)
