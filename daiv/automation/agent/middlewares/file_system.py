from __future__ import annotations

import asyncio
import logging
import re
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

import wcmatch.glob as wcglob
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import DEFAULT_GREP_TIMEOUT, FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, GrepMatch, GrepResult
from deepagents.middleware import filesystem as _upstream_fs_module
from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
    FsToolName,
    GlobSchema,
    GrepSchema,
)
from langchain_core.messages import ToolMessage

from automation.agent.constants import REPO_PATH, SKILLS_CACHE_PATH, SKILLS_PATH, TMP_PATH, WORKSPACE_PATH

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command
    from pydantic import BaseModel

logger = logging.getLogger("daiv.tools")

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

REMINDER_ABSOLUTE_PATHS = "\nTool inputs and outputs use absolute paths (e.g. /workspace/repo/...)."

# Steers the agent to edit_file for existing files, saving the wasted call it otherwise spends
# discovering the rejection. Stated as a preference, not a mechanism: whether an overwrite is refused
# is backend-dependent since deepagents 0.7 (the disk backend replaces the file; the sandbox can
# still answer ALREADY_EXISTS), and one description serves both — see the note on _GREP_DESCRIPTION.
_WRITE_FILE_EXTRA = (
    "IMPORTANT: To modify a file that already exists, use `edit_file`. This tool replaces the entire "
    "file, and on sandbox runs it is rejected outright for a path that already exists. Use it only to "
    "create a new file, or to deliberately rewrite one end to end."
)


def _with_path_reminder(base: str, *extras: str) -> str:
    return "\n".join((base, *extras, REMINDER_ABSOLUTE_PATHS))


# One description for both backends, on purpose. The model's grep description is set once per
# process — via the harness profile for the main agent (``create_deep_agent`` auto-adds its
# FilesystemMiddleware from the globally-registered profile, no per-call override) and via
# ``custom_tool_descriptions`` for subagents — and a run's backend (sandbox ERE vs. disk Python
# ``re``) is only known per-run, so the text cannot branch on it without racing concurrent runs.
# It therefore targets the subset both dialects share: the sandbox runs ``grep -E`` on busybox/musl
# images where Perl-style escapes and lookaround don't work, while Python ``re`` (the disk backend)
# rejects POSIX bracket classes like ``[[:space:]]`` — so the description sticks to anchors,
# alternation, ``[...]`` ranges and escaping, which behave identically on both.
_GREP_DESCRIPTION = r"""Search file contents with a regular expression and return matching files or lines.

The pattern is a REGULAR EXPRESSION (POSIX extended / ERE in sandbox runs; Python `re` on local runs).
Common constructs work: alternation `foo|bar`, anchors `^def `/`;$`, character classes `[A-Z]`,
quantifiers `+ * ? {2,3}`, and groups `(...)`. To match a regex metacharacter literally, escape it
with a backslash, e.g. `def __init__\(self\)` or `value\.attr`.

Avoid non-portable constructs: Perl-style escapes (`\d` `\w` `\s` `\b`), lookaround `(?=...)`, and
backreferences are NOT valid POSIX ERE and will match differently (often nothing) on sandbox runs —
use `[0-9]`, `[A-Za-z0-9_]`, a literal space, and explicit alternation instead.

`output_mode` defaults to `files_with_matches`, which returns FILE PATHS ONLY — no code. To read the
matching lines (tracing a value, checking how a symbol is used, confirming a call site) you must pass
`output_mode="content"`. Reaching for a broader pattern will not turn paths into lines; only the mode does.

Examples:
- Show the matching lines: `grep(pattern="raise [A-Za-z]+Error", output_mode="content")`
- Locate which files match, without their contents: `grep(pattern="TODO")`
- Anchored alternation in Python files: `grep(pattern="^def |^class ", glob="*.py", output_mode="content")`
- Match metacharacters literally (escape them): `grep(pattern="value\.attr", output_mode="content")`
- Count matches per file: `grep(pattern="import", output_mode="count")`

Prefer this tool over shell `grep`/`rg` in bash for searching workspace files."""
GREP_TOOL_DESCRIPTION = _with_path_reminder(_GREP_DESCRIPTION)


# ``_GREP_DESCRIPTION`` (above) overrides only the grep tool's *top-level* description. The model is
# also shown the tool's INPUT SCHEMA, which deepagents builds from a hardcoded ``GrepSchema`` whose
# ``pattern``/``path`` fields still read "literal string, not regex" / "current working directory" —
# a direct contradiction of the regex description that ``custom_tool_descriptions`` cannot reach.
# Both backends grep by regex now, so realign the arg schema in place. The override is process-wide
# and constant (never per-run, so race-free) and reaches the main agent and every subagent alike,
# since they all share this one ``GrepSchema`` class object. Pinned by
# tests/.../test_file_system.py::test_grep_arg_schema_describes_regex so a deepagents bump that
# reworks GrepSchema (or restores the literal wording) fails loudly instead of silently regressing.
_GREP_PATTERN_ARG_DESCRIPTION = "Regular expression to search for (POSIX extended / ERE syntax)."
_GREP_PATH_ARG_DESCRIPTION = "Absolute file or directory to search. Defaults to the workspace root."


def _align_arg_schema(schema_cls: type[BaseModel], overrides: dict[str, str]) -> None:
    """Rewrite a deepagents arg-schema's field descriptions in place.

    deepagents builds each tool's INPUT SCHEMA from a hardcoded Pydantic model that
    ``custom_tool_descriptions`` cannot reach, so its field text can contradict DAIV's overridden tool
    description. The override is process-wide and constant (never per-run, so race-free) and reaches the
    main agent and every subagent alike, since they all share the one schema class object.
    """
    changed = False
    for name, description in overrides.items():
        if (field := schema_cls.model_fields.get(name)) is not None:
            field.description = description
            changed = True
    if changed:
        # Pydantic caches the generated JSON schema; force a rebuild so the new descriptions reach
        # ``model_json_schema()`` — the shape the model is actually shown.
        schema_cls.model_rebuild(force=True)


_align_arg_schema(GrepSchema, {"pattern": _GREP_PATTERN_ARG_DESCRIPTION, "path": _GREP_PATH_ARG_DESCRIPTION})


# Patch the caller's module: ``middleware.filesystem`` bound this name at its own import time, so
# patching ``backends.utils`` is invisible. Both DAIV backends grep by regex, so the hint lies.
def _no_regex_literal_hint(pattern: str) -> str | None:  # noqa: ARG001
    return None


# Soft guard, unlike the hard assert below: ``setattr`` on a module always succeeds, so a renamed
# symbol leaves a dead attribute — but a restored hint only misleads, it never lies about a result.
_REGEX_HINT_PATCH_APPLIED = callable(getattr(_upstream_fs_module, "regex_literal_hint", None))
if _REGEX_HINT_PATCH_APPLIED:
    _upstream_fs_module.regex_literal_hint = _no_regex_literal_hint  # ty: ignore[invalid-assignment]

# Upstream's zero-match body.
_NO_MATCHES_PREFIX = "No matches found"
_GREP_MODE_LABELS = {
    "files_with_matches": "Mode: files_with_matches — matching file paths only, no code.",
    "content": "Mode: content — matching lines.",
    "count": "Mode: count — match count per file.",
}
# Suppressed on a zero-match body: there are no lines to reveal, so re-running is pure churn.
_GREP_MODE_REMEDIES = {"files_with_matches": 'Re-run with output_mode="content" to see the matching lines.'}

# Read the default from the same schema the tool reads it from: hardcoding it would let an upstream
# flip label a content result "files_with_matches", telling the model to re-run a correct search.
_GREP_DEFAULT_OUTPUT_MODE = GrepSchema.model_fields["output_mode"].default

# Import-time parity guard: an unlabelled default would make every bare grep result mis-describe
# itself, which is worse than not labelling at all.
assert _GREP_DEFAULT_OUTPUT_MODE in _GREP_MODE_LABELS, (
    f"deepagents GrepSchema default output_mode is {_GREP_DEFAULT_OUTPUT_MODE!r}, "
    f"which has no label in _GREP_MODE_LABELS ({sorted(_GREP_MODE_LABELS)})."
)


def _label_grep_result(request: ToolCallRequest, result: ToolMessage | Command) -> ToolMessage | Command:
    """Prefix a grep result with the output mode that produced it."""
    if not isinstance(result, ToolMessage) or result.status == "error" or not isinstance(result.content, str):
        return result
    mode = request.tool_call["args"].get("output_mode") or _GREP_DEFAULT_OUTPUT_MODE
    if (label := _GREP_MODE_LABELS.get(mode)) is None:
        return result
    if (remedy := _GREP_MODE_REMEDIES.get(mode)) and not result.content.startswith(_NO_MATCHES_PREFIX):
        label = f"{label} {remedy}"
    return result.model_copy(update={"content": f"{label}\n\n{result.content}"})


class DAIVFilesystemMiddleware(FilesystemMiddleware):
    """deepagents' filesystem middleware, with every grep result labelled by its output mode.

    deepagents renders ``files_with_matches`` (the schema default) as a bare newline-joined path
    list, so a model that wanted matching lines gets no signal that it asked the wrong question and
    reformulates the *pattern* instead of the *mode*. Naming the mode gives it something to
    contradict.

    Carried by the subclass rather than a standalone middleware so the label cannot be wired
    separately from the grep tool it describes: a new call site gets both or neither.
    """

    @property
    def name(self) -> str:
        # ``create_deep_agent`` merges custom middleware by ``.name`` (default: the class name), so
        # our own name would append beside upstream's slot instead of taking it — two fs stacks.
        return FilesystemMiddleware.__name__

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        # super(), not handler(): this same hook is the agent-wide large-result eviction for every
        # tool outside TOOLS_EXCLUDED_FROM_EVICTION (bash, task, MCP). Bypassing it turns that off.
        # Labelling after it keeps the label out of an evicted body, where the model would never see it.
        result = await super().awrap_tool_call(request, handler)
        if request.tool_call["name"] != "grep":
            return result
        return _label_grep_result(request, result)


# deepagents' ``GlobSchema`` ships a ``pattern`` field description carrying a bare `*.txt` example and a
# ``path`` default of "/" that actively mislead: glob's base directory defaults to the FILESYSTEM
# root, not the repository, so a bare repo-relative pattern (`tests/**/*.py`) silently matches
# nothing. The model sees this arg schema alongside the tool description, so realign it in place —
# same process-wide-constant, race-free mechanism as the grep alignment above. Pinned by
# tests/.../test_file_system.py::test_glob_arg_schema_warns_root_anchoring.
_GLOB_PATTERN_ARG_DESCRIPTION = (
    "Glob pattern (supports *, **, ?, [abc], and brace alternation like {py,md}). Lead with `**/` to "
    "match anywhere beneath the search root, e.g. `**/*.py` or `**/test_*.py`."
)
_GLOB_PATH_ARG_DESCRIPTION = (
    "Absolute base directory to search from. Defaults to the filesystem root `/` — which is NOT the "
    "repository, and which reports repository files under more than one path. Set it to the repository "
    "root to scope the search there and get canonical `/workspace/repo/...` paths back."
)
_align_arg_schema(GlobSchema, {"pattern": _GLOB_PATTERN_ARG_DESCRIPTION, "path": _GLOB_PATH_ARG_DESCRIPTION})

_GLOB_EXTRA = (
    "Prefer this tool over shell `find` in bash to locate files by name or pattern inside the "
    "workspace. IMPORTANT: set `path` to the repository root when you mean to search the repository. "
    "Left unset, the search spans the whole workspace and reports the same repository file under more "
    "than one path, only one of which is the canonical `/workspace/repo/...` form you can read back. "
    "Brace alternation works too, e.g. `**/*.{py,md}`. "
    "(Searching outside the workspace, `find`-style `-path` predicates, and piping matches into "
    "`grep` have no glob equivalent — those remain legitimate uses of bash `find`.)"
)
GLOB_TOOL_DESCRIPTION = _with_path_reminder(GLOB_TOOL_DESCRIPTION_BASE, _GLOB_EXTRA)
_LS_EXTRA = (
    "Use this to explore directory layout AND to confirm a path before read_file/edit_file. "
    "`path` is REQUIRED and must be absolute: there is no implicit working directory, so calling `ls` "
    "with no path errors — pass e.g. the repository root. "
    "Prefer this tool over shell `ls` in bash. To list files by pattern or recursively use `glob`, and "
    "to filter by content use `grep`, rather than piping shell `ls` output."
)
LIST_FILES_TOOL_DESCRIPTION = _with_path_reminder(LIST_FILES_TOOL_DESCRIPTION_BASE, _LS_EXTRA)
READ_FILE_TOOL_DESCRIPTION = _with_path_reminder(READ_FILE_TOOL_DESCRIPTION_BASE)
WRITE_FILE_TOOL_DESCRIPTION = _with_path_reminder(WRITE_FILE_TOOL_DESCRIPTION_BASE, _WRITE_FILE_EXTRA)
EDIT_FILE_TOOL_DESCRIPTION = _with_path_reminder(EDIT_FILE_TOOL_DESCRIPTION_BASE)

WRITE_FILE_TOOL = "write_file"
EDIT_FILE_TOOL = "edit_file"
WRITE_TOOL_NAMES = frozenset({WRITE_FILE_TOOL, EDIT_FILE_TOOL})

# A deepagents bump that rewords either prefix would silently disable sandbox sync.
# Pinned by tests/unit_tests/automation/agent/middlewares/test_file_system.py
# (test_upstream_success_prefixes_remain_stable).
WRITE_SUCCESS_PREFIX = "Updated file"
EDIT_SUCCESS_PREFIX = "Successfully replaced"


def filesystem_absolute_path_directive(working_directory: str) -> str:
    """Path directive naming where the repository lives for this run.

    The bare "start with /" rule let the model address repo files with the workspace prefix dropped
    (e.g. ``/daiv/foo`` instead of ``/workspace/repo/daiv/foo``). Neither backend auto-corrects such a
    slip (the sandbox rejects it; disk-backed runs resolve it outside the clone — see
    :meth:`SandboxFileBackend._abs`), so the model must name the full repo path in either mode. This
    states where repo files live (``/workspace/repo/`` in a sandbox, ``/<clone-name>/`` on disk)
    WITHOUT claiming it is the only writable location — the sandbox scratchpad (``/workspace/tmp``)
    and skills (``/workspace/skills``) are also valid.
    """
    root = working_directory.rstrip("/") + "/"
    return (
        "Filesystem tool-call arguments (ls/read_file/edit_file/grep/glob/etc.) MUST be absolute paths. "
        f'Repository files live under "{root}" — address them with the full path (e.g. "{root}path/to/file.py"), '
        f'not a repo-relative path like "/path/to/file.py".'
    )


# Disk-mode fence. The disk composite routes /workspace/repo and /workspace/skills and lets
# everything else under /workspace fall through to the scratch/artifacts backend. These rules keep
# the agent's file tools inside the three real subtrees (repo, skills, tmp), grant read-only access
# to the offloaded-artifact dirs (so eviction read-back works — see below), and deny bare /workspace
# (which would not enumerate the routed subdirs) plus any other path beneath it. First-rule-wins,
# default allow. Sandbox runs do NOT use this — bash is unconstrained there, so fencing only the file
# tools would be inconsistent.
WORKSPACE_FENCE_SUBTREES = [REPO_PATH, f"{REPO_PATH}/**", SKILLS_PATH, f"{SKILLS_PATH}/**", TMP_PATH, f"{TMP_PATH}/**"]

# Offloaded-artifact dirs derived from ``artifacts_root`` (= /workspace in the disk composite).
# deepagents' large-tool-result / conversation-history eviction and git_platform's ``output_to_file``
# all WRITE here through the backend directly (bypassing the fence) and then hand the agent the path
# to read back. Without an explicit read carve-out ahead of the deny, that read-back hits the
# ``/workspace/**`` deny and dead-ends — the full content is written but unrecoverable. Write stays
# denied (the agent never writes here itself; only the framework does, and that bypasses the fence).
# These suffixes mirror deepagents' ``FilesystemMiddleware``; a drift-guard test pins them to the
# framework's computed prefixes so a rename fails loudly instead of silently re-breaking read-back.
WORKSPACE_ARTIFACT_SUBTREES = [
    f"{WORKSPACE_PATH}/large_tool_results",
    f"{WORKSPACE_PATH}/large_tool_results/**",
    f"{WORKSPACE_PATH}/conversation_history",
    f"{WORKSPACE_PATH}/conversation_history/**",
]

WORKSPACE_FENCE_PERMISSIONS = [
    FilesystemPermission(operations=["read", "write"], paths=WORKSPACE_FENCE_SUBTREES, mode="allow"),
    FilesystemPermission(operations=["read"], paths=WORKSPACE_ARTIFACT_SUBTREES, mode="allow"),
    FilesystemPermission(operations=["read", "write"], paths=[WORKSPACE_PATH, f"{WORKSPACE_PATH}/**"], mode="deny"),
]

# Enforced against the validated path, so an upstream tool rename cannot restore write access.
READ_ONLY_PERMISSIONS: list[FilesystemPermission] = [
    FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")
]

CUSTOM_TOOL_DESCRIPTIONS = {
    "grep": GREP_TOOL_DESCRIPTION,
    "glob": GLOB_TOOL_DESCRIPTION,
    "ls": LIST_FILES_TOOL_DESCRIPTION,
    "read_file": READ_FILE_TOOL_DESCRIPTION,
    WRITE_FILE_TOOL: WRITE_FILE_TOOL_DESCRIPTION,
    EDIT_FILE_TOOL: EDIT_FILE_TOOL_DESCRIPTION,
}

# Explicit allowlists for ``FilesystemMiddleware(tools=...)``. Two upstream tools are
# deliberately absent from both:
#
# - ``delete``: 0.7 auto-exposes a recursive (``shutil.rmtree``) delete, but the sandbox's
#   ``fs_delete`` RPC removes a single file only, so the tool would promise the model
#   directory removal the backend cannot perform. Add it once daiv-sandbox supports it.
# - ``execute``: DAIV's shell is ``SandboxMiddleware``'s ``bash``; no DAIV backend implements
#   ``SandboxBackendProtocol``, so upstream's ``execute`` has no working implementation here.
#
# An allowlist (rather than permission rules) is provider-independent and drops the tools from
# the ``ToolNode`` entirely, so neither is reachable even if a permission rule later widens.
WORKSPACE_FS_TOOLS: list[FsToolName] = ["ls", "read_file", "write_file", "edit_file", "glob", "grep"]

# Read-only subagents (code-review detectors, explore). Paired with, not a replacement for,
# their ``_permissions`` deny rules: the allowlist hides the write tools while the rules stay
# authoritative for path scoping.
READ_ONLY_FS_TOOLS: list[FsToolName] = ["ls", "read_file", "glob", "grep"]


# ---------------------------------------------------------------------------
# Backends
#
# Thin daiv-side extensions to deepagents' backends. Two methods that
# ``BackendProtocol`` doesn't expose:
#
# - ``unlink(path)``: drop a single file (``Path.unlink`` under the hood).
# - ``stat_mode(path)``: report a file's POSIX mode bits.
#
# ``DAIVBackendProtocol`` formalises the surface and ``DAIVCompositeBackend`` asserts
# every routed backend implements it at construction time, so a new backend is a
# matter of subclassing the deepagents primitive and supplying these two methods.
#
# ``unlink`` is deliberately not named ``delete``: deepagents 0.7 added its own
# ``delete``/``adelete`` to ``BackendProtocol``, and ``adelete`` delegates to
# ``self.delete`` via ``asyncio.to_thread`` — an async override would be handed back
# as an un-awaited coroutine. Upstream's ``delete`` is also recursive over directories,
# which the sandbox's file-only ``fs_delete`` RPC cannot honour.
# ---------------------------------------------------------------------------


class DAIVFilesystemBackend(FilesystemBackend):
    """``FilesystemBackend`` plus DAIV's two backend-protocol extensions
    (``unlink`` and ``stat_mode``)."""

    def _to_path(self, virtual_path: str) -> Path:
        return Path(self._resolve_path(virtual_path))

    async def unlink(self, virtual_path: str) -> bool:
        try:
            await asyncio.to_thread(self._to_path(virtual_path).unlink, missing_ok=True)
        except OSError:
            logger.exception("disk unlink failed for %s", virtual_path)
            return False
        return True

    async def stat_mode(self, virtual_path: str) -> int:
        try:
            st = await asyncio.to_thread(self._to_path(virtual_path).stat)
        except OSError:
            logger.exception("disk stat failed for %s; mirroring with 0o644 fallback", virtual_path)
            return 0o644
        return stat.S_IMODE(st.st_mode)

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
                try:
                    if not fp.is_file():
                        continue
                except OSError, RuntimeError:
                    continue
                if glob_matcher is not None and not glob_matcher.match(str(fp.relative_to(root))):
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
            # in the operator logs rather than setting ``GrepResult.error`` — ``DAIVCompositeBackend``
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


@runtime_checkable
class DAIVBackendProtocol(Protocol):
    """The two methods DAIV adds on top of ``BackendProtocol``: ``unlink`` (drop a single
    file) and ``stat_mode`` (report POSIX mode bits).

    The composite asserts on this shape so a misconfigured route fails loudly at
    construction time instead of with a runtime ``AttributeError`` on first unlink.
    Defined as its own ``Protocol`` rather than extending ``BackendProtocol`` because
    deepagents' base isn't a typing ``Protocol``.
    """

    async def unlink(self, virtual_path: str) -> bool: ...

    async def stat_mode(self, virtual_path: str) -> int: ...


def _require_daiv_backend(backend: BackendProtocol, label: str) -> None:
    """Raise ``TypeError`` unless ``backend`` implements ``DAIVBackendProtocol``.

    A silent ``AttributeError`` on the first ``unlink``/``stat_mode`` is worse than a
    loud failure at wiring time, so both the constructor and ``add_route`` gate on this.
    """
    if not isinstance(backend, DAIVBackendProtocol):
        raise TypeError(
            f"{label} requires a backend implementing DAIVBackendProtocol (unlink + stat_mode); "
            f"{type(backend).__name__} does not."
        )


class DAIVCompositeBackend(CompositeBackend):
    """``CompositeBackend`` with the two DAIV extensions (``unlink``/``stat_mode``) and a
    ``resolve_backend_for`` helper for callers that need to dispatch on the underlying
    backend type (``isinstance``-style routing — e.g. the gitignore guard).

    Routing-aware ``unlink``/``stat_mode`` strip the route prefix before delegating, so
    underlying backends see the same key shape they would receive through any other
    composite-routed call (``aupload_files``, ``adownload_files``, etc.).

    Asserts on construction that every wired backend implements ``DAIVBackendProtocol``;
    silent ``AttributeError`` on first rollback is worse than a startup crash.
    """

    def __init__(
        self, default: BackendProtocol, routes: dict[str, BackendProtocol], *, artifacts_root: str = "/"
    ) -> None:
        for label, backend in (("default", default), *routes.items()):
            _require_daiv_backend(backend, f"DAIVCompositeBackend route {label!r}")
        super().__init__(default=default, routes=routes, artifacts_root=artifacts_root)

    async def unlink(self, virtual_path: str) -> bool:
        backend, stripped = self._get_backend_and_key(virtual_path)
        return await cast("DAIVBackendProtocol", backend).unlink(stripped)

    async def stat_mode(self, virtual_path: str) -> int:
        backend, stripped = self._get_backend_and_key(virtual_path)
        return await cast("DAIVBackendProtocol", backend).stat_mode(stripped)

    def resolve_backend_for(self, virtual_path: str) -> BackendProtocol:
        """Return the underlying backend that owns ``virtual_path``.

        Used by callers that need to dispatch on the underlying backend shape.
        """
        backend, _ = self._get_backend_and_key(virtual_path)
        return backend


def build_disk_workspace_backend(clone_dir: Path, *, skills_cache: Path = SKILLS_CACHE_PATH) -> DAIVCompositeBackend:
    """Build the disk-backed composite that serves the unified ``/workspace`` namespace.

    Routes:
      - ``/workspace/repo/``   → the local git clone (``clone_dir``)
      - ``/workspace/skills/`` → the shared global skills cache (``SKILLS_CACHE_PATH``)
      - everything else under ``/workspace`` (the ``/workspace/tmp`` scratchpad and the offloaded
        artifact dirs derived from ``artifacts_root``) → a per-run scratch backend rooted at the
        clone's parent (the ``set_runtime_ctx`` tempdir, auto-removed at run end). Non-routed paths
        reach this default with their full path, so they materialise under ``<parent>/workspace/``,
        siblings to the clone and never committed.

    The skills cache is a route (not copied per run) so the global-cache idempotency in
    ``SkillsMiddleware`` is preserved.
    """
    repo_backend = DAIVFilesystemBackend(root_dir=clone_dir, virtual_mode=True)
    skills_backend = DAIVFilesystemBackend(root_dir=skills_cache, virtual_mode=True)
    scratch_backend = DAIVFilesystemBackend(root_dir=clone_dir.parent, virtual_mode=True)
    return DAIVCompositeBackend(
        default=scratch_backend,
        routes={f"{REPO_PATH}/": repo_backend, f"{SKILLS_PATH}/": skills_backend},
        artifacts_root=WORKSPACE_PATH,
    )
