from __future__ import annotations

import asyncio
import base64
import logging
import re
import stat
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

import httpx
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    BackendProtocol,
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import FilesystemPermission

from automation.agent.constants import REPO_PATH, SKILLS_CACHE_PATH, SKILLS_PATH, TMP_PATH, WORKSPACE_PATH
from core.sandbox.client import DAIVSandboxClient, is_transient_sandbox_error
from core.sandbox.schemas import (
    FsDeleteRequest,
    FsEditRequest,
    FsError,
    FsErrorCode,
    FsGlobRequest,
    FsGrepRequest,
    FsLsRequest,
    FsReadRequest,
    FsWriteRequest,
    RunCommandsRequest,
    RunCommandsResponse,
)

logger = logging.getLogger("daiv.tools")

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

REMINDER_ABSOLUTE_PATHS = """
IMPORTANT:
- Tool inputs/outputs use absolute paths (e.g. /workspace/repo/...).
- DO NOT output these absolute paths to the user.
- Convert to repo-relative paths in all user-visible text.
"""

# The agent is calling write_file on a file that already exists, gets rejected
# ("Cannot write because it already exists"), then correctly switches to edit_file. This wastes one tool call.
_WRITE_FILE_EXTRA = (
    "IMPORTANT: This tool can ONLY create new files. It will fail on files that already exist. "
    "To modify existing files, always use `edit_file` instead."
)


def _with_path_reminder(base: str, *extras: str) -> str:
    return "\n".join((base, *extras, REMINDER_ABSOLUTE_PATHS))


_GREP_DESCRIPTION = r"""Search file contents with a regular expression and return matching files or lines.

The pattern is a REGULAR EXPRESSION (POSIX extended / ERE in sandbox runs; Python `re` on local runs).
Common constructs work: alternation `foo|bar`, anchors `^def `/`;$`, character classes `[A-Z]`,
quantifiers `+ * ? {2,3}`, and groups `(...)`. To match a regex metacharacter literally, escape it
with a backslash, e.g. `def __init__\(self\)` or `value\.attr`.

Parameters:
- pattern: the regular expression.
- path: absolute file or directory to search (defaults to the workspace root).
- glob: optional filename glob to restrict the search (e.g. `*.py`).
- output_mode: `files_with_matches` (default), `content`, or `count`.

Prefer this tool over shell `grep`/`rg` in bash for searching workspace files."""
GREP_TOOL_DESCRIPTION = _with_path_reminder(_GREP_DESCRIPTION)
GLOB_TOOL_DESCRIPTION = _with_path_reminder(GLOB_TOOL_DESCRIPTION_BASE)
LIST_FILES_TOOL_DESCRIPTION = _with_path_reminder(LIST_FILES_TOOL_DESCRIPTION_BASE)
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
    (e.g. ``/daiv/foo`` instead of ``/workspace/repo/daiv/foo``); in a sandbox run the backend now
    resolves such slips under the repo root (:meth:`SandboxFileBackend._abs`), but a slip is still
    ambiguous (it could land on the wrong file), and disk-backed runs do NOT auto-correct it (the
    path resolves outside the clone), so the model must name the full repo path in either mode. This
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

CUSTOM_TOOL_DESCRIPTIONS = {
    "grep": GREP_TOOL_DESCRIPTION,
    "glob": GLOB_TOOL_DESCRIPTION,
    "ls": LIST_FILES_TOOL_DESCRIPTION,
    "read_file": READ_FILE_TOOL_DESCRIPTION,
    WRITE_FILE_TOOL: WRITE_FILE_TOOL_DESCRIPTION,
    EDIT_FILE_TOOL: EDIT_FILE_TOOL_DESCRIPTION,
}


# ---------------------------------------------------------------------------
# Backends
#
# Thin daiv-side extensions to deepagents' backends. Two methods that
# ``BackendProtocol`` doesn't expose:
#
# - ``delete(path)``: drop a file (``Path.unlink`` under the hood).
# - ``stat_mode(path)``: report a file's POSIX mode bits.
#
# ``DAIVBackendProtocol`` formalises the surface and ``DAIVCompositeBackend`` asserts
# every routed backend implements it at construction time, so a new backend is a
# matter of subclassing the deepagents primitive and supplying these two methods.
# ---------------------------------------------------------------------------


class DAIVFilesystemBackend(FilesystemBackend):
    """``FilesystemBackend`` plus DAIV's two backend-protocol extensions
    (``delete`` and ``stat_mode``)."""

    def _to_path(self, virtual_path: str) -> Path:
        return Path(self._resolve_path(virtual_path))

    async def delete(self, virtual_path: str) -> bool:
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

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Regex grep (convergence with the sandbox's ERE search and Claude Code).

        Validates the pattern with Python ``re`` up front so an invalid regex is a clean,
        model-fixable error rather than a silent zero-match (the inherited backend greps literally
        via ``rg -F``/``re.escape``; ripgrep also exits 2 quietly on a bad regex). Reuses the
        parent's ``_python_search`` with the *raw* (unescaped) pattern, which compiles it as a
        regex — trading ripgrep's speed for correct semantics on local/disk runs (the deployed
        path is the sandbox backend).
        """
        try:
            re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"invalid regular expression: {pattern!r} ({exc})")
        return await asyncio.to_thread(self._regex_grep, pattern, path, glob)

    def _regex_grep(self, pattern: str, path: str | None, glob: str | None) -> GrepResult:
        try:
            base_full = self._resolve_path(path or ".")
        except ValueError:
            return GrepResult(matches=[])
        except (OSError, RuntimeError) as exc:
            return GrepResult(error=f"Error searching path '{path or '.'}': {exc}", matches=[])
        try:
            if not base_full.exists():
                return GrepResult(matches=[])
        except OSError as exc:
            return GrepResult(error=f"Error searching path '{path or '.'}': {exc}", matches=[])
        results = self._python_search(pattern, base_full, glob)
        matches = [
            GrepMatch(path=fpath, line=int(line_num), text=line_text)
            for fpath, items in results.items()
            for (line_num, line_text) in items
        ]
        return GrepResult(matches=matches)


@runtime_checkable
class DAIVBackendProtocol(Protocol):
    """The two methods DAIV adds on top of ``BackendProtocol``: ``delete`` (drop a file)
    and ``stat_mode`` (report POSIX mode bits).

    The composite asserts on this shape so a misconfigured route fails loudly at
    construction time instead of with a runtime ``AttributeError`` on first delete.
    Defined as its own ``Protocol`` rather than extending ``BackendProtocol`` because
    deepagents' base isn't a typing ``Protocol``.
    """

    async def delete(self, virtual_path: str) -> bool: ...

    async def stat_mode(self, virtual_path: str) -> int: ...


def _require_daiv_backend(backend: BackendProtocol, label: str) -> None:
    """Raise ``TypeError`` unless ``backend`` implements ``DAIVBackendProtocol``.

    A silent ``AttributeError`` on the first ``delete``/``stat_mode`` is worse than a
    loud failure at wiring time, so both the constructor and ``add_route`` gate on this.
    """
    if not isinstance(backend, DAIVBackendProtocol):
        raise TypeError(
            f"{label} requires a backend implementing DAIVBackendProtocol (delete + stat_mode); "
            f"{type(backend).__name__} does not."
        )


class DAIVCompositeBackend(CompositeBackend):
    """``CompositeBackend`` with the two DAIV extensions (``delete``/``stat_mode``) and a
    ``resolve_backend_for`` helper for callers that need to dispatch on the underlying
    backend type (``isinstance``-style routing — e.g. the gitignore guard).

    Routing-aware ``delete``/``stat_mode`` strip the route prefix before delegating, so
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

    async def delete(self, virtual_path: str) -> bool:
        backend, stripped = self._get_backend_and_key(virtual_path)
        return await cast("DAIVBackendProtocol", backend).delete(stripped)

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


# Every fs *soft* failure arrives in the 200 body as a structured ``FsError`` (the sandbox no longer
# raises HTTP 400 for a bad path on ls/grep/glob — that is an ``invalid_path`` error in the body like
# every other op), mapped to the agent below via ``_fs_error_text``.
#
# A non-200 carries no structured body — it is a transport/HTTP fault (the per-session-lock 409
# "Session is busy", a request timeout, a 5xx, or no response at all), which the model cannot fix by
# changing its arguments. The client's ``raise_for_status`` turns these into ``httpx.HTTPError``;
# rather than let one abort the whole run, each agent-facing method below catches it and returns a
# soft result error via ``_fs_transport_failure_text``, mirroring the bash tool's transient/permanent
# split (see ``BashFailure``): a transient fault invites one retry, a permanent one tells the agent
# the file tools are unusable for the rest of the run.
#
# Codes that point the agent at a *different tool* get a DAIV-authored routing hint; the rest fall
# through to the sandbox ``message``, which already carries the actionable detail (edit retry hints,
# the offending offset, the rejected path, the underlying failure). Distinct codes stay distinct —
# they are never collapsed into a single generic "operation failed".
#
# Each hint (and the fall-through ``message``) is a sentence *fragment* meant to read as the tail of
# a ``"<path>": `` prefix the call site supplies (e.g. ``File '/x': is a directory — …``). Phrase new
# hints/messages to follow that prefix, not as standalone capitalised sentences.
_FS_CODE_HINTS: dict[FsErrorCode, str] = {
    FsErrorCode.NOT_FOUND: "does not exist",
    FsErrorCode.IS_A_DIRECTORY: "is a directory — list it with the ls/glob tools, not read_file/edit_file",
    FsErrorCode.NOT_A_DIRECTORY: "is not a directory — read it with read_file, not the ls/glob tools",
    FsErrorCode.ALREADY_EXISTS: "already exists — modify it with edit_file (write_file only creates new files)",
    FsErrorCode.NOT_A_TEXT_FILE: "is not a UTF-8 text file and cannot be edited",
    FsErrorCode.INVALID_PATTERN: (
        "is not a valid regular expression — fix the syntax, or escape regex metacharacters "
        "(e.g. \\( \\. \\|) to match them literally"
    ),
}


def _fs_error_text(error: FsError) -> str:
    """Render a structured sandbox error as an actionable, agent-facing string."""
    return _FS_CODE_HINTS.get(error.code, error.message)


# Tails for a sandbox transport/HTTP fault (no structured body), phrased to read after the per-op
# ``"<op> '<arg>': "`` prefix each method builds, exactly like ``_fs_error_text``. The transient text
# is kept free of status codes / per-call detail so identical retries read identically to the model.
# "may or may not have run" (not "did not run"): a busy-409 is raised at lock acquisition so the op
# provably never ran, but a transient transport error (a lost-response timeout) can also reach here,
# and on a mutating op (write/edit) the request may have executed before the response was lost. Match
# the bash tool's hedge (``_BASH_TRANSIENT_ERROR``) rather than make a false "did not run" claim.
_FS_TRANSPORT_TRANSIENT_TEXT = (
    "the workspace was momentarily busy or unreachable, so the operation may or may not have run — "
    "this is usually transient; retry the same call once."
)
_FS_TRANSPORT_PERMANENT_TEXT = (
    "the workspace is unavailable for the rest of this run (the sandbox rejected the call in a way a "
    "retry will not fix); stop using the file tools and verify your work by other means."
)


def _fs_transport_failure_text(exc: httpx.HTTPError, op: str, target: str) -> str:
    """Log a sandbox transport/HTTP fault and render it as an actionable, agent-facing string.

    Returning the fault as a soft result (instead of letting it propagate) would otherwise drop the
    only record of it — the client raises without logging — so log here first: a transient
    (retryable) fault at WARNING, a permanent one at ERROR with the traceback, so a genuine
    infra/auth/session-gone fault still reaches the logs (and Sentry) rather than vanishing into a
    tool message. The returned text is the tail of the per-op ``"<op> '<arg>': "`` prefix the caller
    builds (transient ⇒ retry once; permanent ⇒ the file tools are unusable for the rest of the run).
    """
    if is_transient_sandbox_error(exc):
        logger.warning("Sandbox %s transport failure for %r (transient, retryable): %s", op, target, exc)
        return _FS_TRANSPORT_TRANSIENT_TEXT
    logger.error("Sandbox %s transport failure for %r (permanent)", op, target, exc_info=exc)
    return _FS_TRANSPORT_PERMANENT_TEXT


class SandboxFileBackend(BackendProtocol):
    """Deepagents backend whose files live in a sandbox workspace, and the run's
    command-execution handle (``run_commands``).

    The agent addresses files by their sandbox-absolute path (``/workspace/repo``,
    ``/workspace/skills``, ``/workspace/tmp``); the backend is a thin proxy to
    ``DAIVSandboxClient`` — the sandbox is authoritative, so there is no local mirror.
    Paths already under ``/workspace`` pass through unchanged; the only translation is in
    :meth:`_abs`, which maps the virtual root ``/`` (and repo paths sent with the workspace
    prefix dropped) onto the workspace/repo root so they don't error on the sandbox. Every op
    is one RPC over ``DAIVSandboxClient``; there is no local copy, so no rollback/desync machinery.

    The client is supplied at construction; the backend is **bound** to the run's session via
    :meth:`bind_session` once ``SandboxMiddleware.abefore_agent`` has started (or reused) it. Any
    file op before binding raises ``RuntimeError`` (a programming error — the middleware must bind
    first).

    Only the async methods are implemented — the async agent path never calls the
    sync ones (the inherited sync methods raise ``NotImplementedError``; a sync call
    here would be a programming error). ``delete`` and ``stat_mode`` round out
    ``DAIVBackendProtocol``; ``stat_mode`` returns a constant since the sandbox is
    authoritative (no mirror to a local repo), so exact mode bits are irrelevant.

    Note: ``awrite`` writes files at a fixed ``0o644`` (the file tools don't carry a mode), so
    an executable bit must be set via ``bash`` (``chmod +x``) in the sandbox, not through the
    file tools. ``aedit`` carries no mode (``FsEditRequest`` has no mode field), so the sandbox
    edits in place and leaves the existing file's mode untouched.
    """

    def __init__(self, *, client: DAIVSandboxClient | None = None, session_id: str | None = None) -> None:
        self._client = client
        self._session_id = session_id

    def bind_session(self, session_id: str) -> None:
        """Attach the run's session id. The client is supplied at construction; this only sets the
        workspace (session). Subagents share the parent's backend instance, so re-binding the *same*
        session is a no-op; re-binding to a *different* session is a programming error and raises,
        rather than silently redirecting every file op to another workspace.
        """
        if self._session_id is not None and self._session_id != session_id:
            raise RuntimeError(
                f"SandboxFileBackend is already bound to session {self._session_id!r}; "
                f"refusing to rebind to {session_id!r}"
            )
        self._session_id = session_id

    def _require_bound(self) -> tuple[DAIVSandboxClient, str]:
        if self._client is None or not self._session_id:
            raise RuntimeError("SandboxFileBackend is not bound to a sandbox session")
        return self._client, self._session_id

    async def run_commands(self, commands: list[str], *, fail_fast: bool) -> RunCommandsResponse:
        """Run shell commands in the bound session's workspace.

        The run's command-execution handle (used by the ``bash`` tool and sandbox-mode
        ``GitManager``). A thin pass-through to ``DAIVSandboxClient.run_commands`` — it takes a
        *list* + ``fail_fast`` (not a single command) so multi-command batches run in one
        round-trip. Like the other methods here it **raises** on transport/HTTP errors;
        callers that need graceful degradation (the ``bash`` tool) wrap it.

        Intentionally NOT deepagents' ``SandboxBackendProtocol.aexecute``: implementing that
        protocol would activate deepagents' always-registered, ungated ``execute`` tool.
        """
        client, session_id = self._require_bound()
        return await client.run_commands(session_id, RunCommandsRequest(commands=commands, fail_fast=fail_fast))

    # -- path mapping -------------------------------------------------------
    # The sandbox is authoritative and the agent addresses files by their
    # sandbox-absolute path (/workspace/repo, /workspace/skills, /workspace/tmp).
    # The sandbox rejects anything outside WORKSPACE_PATH (an ``invalid_path`` error), so two
    # common model inputs need normalising before they reach the wire:
    #   - the deepagents virtual root "/" (a path-less glob/grep/ls default) → the
    #     workspace root, so those defaults search /workspace rather than being rejected;
    #   - a repo path with the workspace prefix dropped (e.g. "/daiv/foo" instead of
    #     "/workspace/repo/daiv/foo") → resolved under the repo root, so a common slip
    #     lands on the intended file instead of failing.
    # Paths already under /workspace pass straight through unchanged.
    def _abs(self, backend_path: str) -> str:
        if not backend_path or backend_path == "/":
            return WORKSPACE_PATH
        if backend_path == WORKSPACE_PATH or backend_path.startswith(f"{WORKSPACE_PATH}/"):
            return backend_path
        return f"{REPO_PATH}/{backend_path.lstrip('/')}"

    def _rel(self, abs_path: str) -> str:
        return abs_path or "/"

    # -- async protocol methods ---------------------------------------------
    # The Fs*Response types carry a structured ``error`` (an ``FsError`` with a stable ``code``),
    # populated alongside an empty list on a soft sandbox failure returned as 200. Map it into the
    # deepagents result's ``error`` so the filesystem middleware surfaces an actionable message to
    # the model. A missing path now carries ``code=not_found`` (distinct from an empty directory /
    # no match, which has ``error=None``), so absence reads as "does not exist" instead of a clean
    # "empty directory / no matches".
    async def als(self, path: str) -> LsResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_ls(session_id, FsLsRequest(path=self._abs(path)))
        except httpx.HTTPError as exc:
            return LsResult(error=f"Listing '{path}': {_fs_transport_failure_text(exc, 'ls', path)}")
        if resp.error is not None:
            return LsResult(error=f"Listing '{path}': {_fs_error_text(resp.error)}")
        return LsResult(entries=[FileInfo(path=self._rel(e.path), is_dir=e.is_dir) for e in resp.entries])

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_read(
                session_id, FsReadRequest(path=self._abs(file_path), offset=offset, limit=limit)
            )
        except httpx.HTTPError as exc:
            return ReadResult(error=f"File '{file_path}': {_fs_transport_failure_text(exc, 'read', file_path)}")
        if resp.error is not None:
            return ReadResult(error=f"File '{file_path}': {_fs_error_text(resp.error)}")
        return ReadResult(file_data=FileData(content=resp.content or "", encoding=resp.encoding or "utf-8"))

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_grep(
                session_id, FsGrepRequest(pattern=pattern, path=self._abs(path or "/"), glob=glob)
            )
        except httpx.HTTPError as exc:
            return GrepResult(error=f"Grep '{pattern}': {_fs_transport_failure_text(exc, 'grep', pattern)}")
        if resp.error is not None:
            return GrepResult(error=f"Grep '{pattern}': {_fs_error_text(resp.error)}")
        matches = [GrepMatch(path=self._rel(m.path), line=m.line, text=m.text) for m in resp.matches]
        if resp.truncated:
            logger.warning("grep results truncated for pattern %r under %s", pattern, path)
            matches.append(
                GrepMatch(
                    path="(grep results truncated)",
                    line=0,
                    text=(
                        f"Showing the first {len(resp.matches)} matches. Narrow the path, add a glob, "
                        "or use a more specific pattern to see the rest."
                    ),
                )
            )
        return GrepResult(matches=matches)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_glob(session_id, FsGlobRequest(pattern=pattern, path=self._abs(path)))
        except httpx.HTTPError as exc:
            return GlobResult(error=f"Glob '{pattern}': {_fs_transport_failure_text(exc, 'glob', pattern)}")
        if resp.error is not None:
            return GlobResult(error=f"Glob '{pattern}': {_fs_error_text(resp.error)}")
        return GlobResult(matches=[FileInfo(path=self._rel(e.path), is_dir=e.is_dir) for e in resp.matches])

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_write(
                session_id,
                FsWriteRequest(
                    path=self._abs(file_path), content=base64.b64encode(content.encode("utf-8")), mode=0o644
                ),
            )
        except httpx.HTTPError as exc:
            return WriteResult(
                error=f"Failed to write file '{file_path}': {_fs_transport_failure_text(exc, 'write', file_path)}"
            )
        if resp.error is not None:
            return WriteResult(error=f"Failed to write file '{file_path}': {_fs_error_text(resp.error)}")
        return WriteResult(path=file_path)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        client, session_id = self._require_bound()
        try:
            resp = await client.fs_edit(
                session_id,
                FsEditRequest(path=self._abs(file_path), old=old_string, new=new_string, replace_all=replace_all),
            )
        except httpx.HTTPError as exc:
            return EditResult(
                error=f"Error editing file '{file_path}': {_fs_transport_failure_text(exc, 'edit', file_path)}"
            )
        if resp.error is not None:
            return EditResult(error=f"Error editing file '{file_path}': {_fs_error_text(resp.error)}")
        return EditResult(path=file_path, occurrences=resp.occurrences if resp.occurrences is not None else 1)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        client, session_id = self._require_bound()
        out: list[FileUploadResponse] = []
        for path, data in files:
            resp = await client.fs_write(
                session_id, FsWriteRequest(path=self._abs(path), content=base64.b64encode(data), mode=0o644)
            )
            # deepagents annotates ``error`` as the narrow ``FileOperationError`` literal but
            # documents accepting backend-specific strings; the sandbox returns its own messages.
            error = None if resp.error is None else _fs_error_text(resp.error)
            out.append(FileUploadResponse(path=path, error=error))  # ty: ignore[invalid-argument-type]
        return out

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        client, session_id = self._require_bound()
        out: list[FileDownloadResponse] = []
        for path in paths:
            resp = await client.fs_read(session_id, FsReadRequest(path=self._abs(path)))
            if resp.error is not None and resp.error.code == FsErrorCode.NOT_FOUND:
                # Normalise absence onto deepagents' FILE_NOT_FOUND sentinel so its callers can branch
                # on it. (The old code compared the raw error string to this sentinel, which silently
                # stopped matching once the wire error became a structured object.)
                out.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
            elif resp.error is not None:
                # See ``aupload_files``: deepagents accepts backend-specific error strings.
                out.append(
                    FileDownloadResponse(path=path, error=_fs_error_text(resp.error))  # ty: ignore[invalid-argument-type]
                )
            elif resp.encoding == "base64":
                out.append(FileDownloadResponse(path=path, content=base64.b64decode(resp.content or "")))
            else:
                out.append(FileDownloadResponse(path=path, content=(resp.content or "").encode("utf-8")))
        return out

    # -- DAIVBackendProtocol -------------------------------------------------
    async def delete(self, virtual_path: str) -> bool:
        client, session_id = self._require_bound()
        # ``delete``'s protocol return is a bare bool with no error channel, so any failure — a
        # transport fault or a sandbox-reported reason — can only be reported as ``False``. Log it
        # first in both branches so a failed delete is diagnosable rather than a silent ``False``.
        try:
            resp = await client.fs_delete(session_id, FsDeleteRequest(path=self._abs(virtual_path)))
        except httpx.HTTPError as exc:
            logger.warning("Sandbox delete transport failure for %s: %s", virtual_path, exc)
            return False
        if resp.error is not None:
            logger.warning("Sandbox delete failed for %s: %s", virtual_path, _fs_error_text(resp.error))
            return False
        if not resp.removed:
            # Idempotent success: the path was already absent. Match ``DAIVFilesystemBackend.delete``
            # (``unlink(missing_ok=True)``), which also reports success for a no-op delete.
            logger.debug("Sandbox delete: %s was already absent (nothing removed)", virtual_path)
        return True

    async def stat_mode(self, virtual_path: str) -> int:
        return 0o644
