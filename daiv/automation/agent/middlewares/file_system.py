from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import wcmatch.glob as wcglob
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.utils import sanitize_tool_call_id
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

from automation.agent.constants import SKILLS_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deepagents.backends.protocol import BackendProtocol
    from langchain.tools import ToolRuntime
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langgraph.types import Command
    from pydantic import BaseModel

    from automation.agent.context import RuntimeCtx

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

# Upstream's permission matchers glob without DOTGLOB, so ``deny write /**`` would let ``.git`` and ``.github`` through.
# They read this global at call time; a rename must fail here, not reopen every dot path.
if not isinstance(getattr(_upstream_fs_module, "_FS_WCMATCH_FLAGS", None), int):
    raise ImportError("deepagents.middleware.filesystem no longer defines _FS_WCMATCH_FLAGS")
_upstream_fs_module._FS_WCMATCH_FLAGS |= wcglob.DOTGLOB  # ty: ignore[invalid-assignment]

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
        # A skill body is instructions the model must read whole, so it skips eviction. Every other tool goes
        # through super(): this hook is the agent-wide large-result eviction (bash, task, MCP).
        if request.tool_call["name"] == SKILLS_TOOL_NAME:
            return await handler(request)
        # Labelling after super() keeps the label out of an evicted body, where the model would never see it.
        result = await super().awrap_tool_call(request, handler)
        if request.tool_call["name"] != "grep":
            return result
        return _label_grep_result(request, result)


_PREVIEW_MAX_LINES = 25
_PREVIEW_MAX_CHARS = 1024


def large_tool_results_prefix(backend: BackendProtocol) -> str:
    """The directory ``FilesystemMiddleware`` evicts a too-large tool result to, for ``backend``.

    Derived exactly as upstream's ``__init__`` derives it (``artifacts_root`` is honoured only for a
    ``CompositeBackend``), so a tool that writes its result to a file on request lands it beside the
    auto-evicted ones, under the same ``tool_call_id`` naming: one place for the agent to read them back.
    """
    artifacts_root = backend.artifacts_root if isinstance(backend, CompositeBackend) else "/"
    return f"{artifacts_root.rstrip('/')}/large_tool_results"


def _file_write_confirmation(path: str, byte_count: int, line_count: int, output: str) -> str:
    """Compact confirmation returned in place of the written content: path, size, head preview."""
    preview = "\n".join(output.splitlines()[:_PREVIEW_MAX_LINES])
    if len(preview) > _PREVIEW_MAX_CHARS:
        preview = preview[:_PREVIEW_MAX_CHARS] + "\n… (preview truncated)"
    shown = min(line_count, _PREVIEW_MAX_LINES)
    return f"Wrote {byte_count} bytes ({line_count} lines) to {path}\nPreview (first {shown} lines):\n{preview}"


async def write_tool_result_to_file(
    output: str, *, runtime: ToolRuntime[RuntimeCtx], backend: BackendProtocol, tool_name: str
) -> str:
    """Write a tool's full result under ``backend``'s :func:`large_tool_results_prefix`, keyed by ``tool_call_id``.

    Writes through the backend the agent's ``read_file``/``grep`` use, so the file is addressable at once.
    Returns a compact confirmation, or an ``error: ...`` string when the write fails.
    """
    if not runtime.tool_call_id:
        # A shared fallback filename would silently overwrite an earlier dump.
        logger.error("[%s] Cannot write result to file: missing tool_call_id", tool_name)
        return "error: Failed to write result to file — missing tool_call_id."
    path = f"{large_tool_results_prefix(backend)}/{sanitize_tool_call_id(runtime.tool_call_id)}"
    try:
        result = await backend.awrite(path, output)
    except Exception as exc:
        logger.exception("[%s] Failed to write result to %s", tool_name, path)
        return f"error: Failed to write result to {path}. Details: {exc}"
    if result.error:
        logger.error("[%s] Backend rejected write to %s: %s", tool_name, path, result.error)
        return f"error: Failed to write result to {path}. Details: {result.error}"
    return _file_write_confirmation(path, len(output.encode("utf-8")), len(output.splitlines()), output)


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
