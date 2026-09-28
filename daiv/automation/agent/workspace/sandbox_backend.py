"""The ``/workspace`` backend of a sandbox-enabled run: every file op is one RPC to the run's sandbox session."""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, NamedTuple

import httpx
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
from deepagents.middleware.filesystem import EMPTY_CONTENT_WARNING

from automation.agent.constants import WORKSPACE_PATH
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

if TYPE_CHECKING:
    from automation.agent.workspace.session import SandboxSession

logger = logging.getLogger("daiv.tools")


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
# the ``"<op> '<arg>': "`` prefix the call site supplies (e.g. ``File '/x': is a directory — …``).
# ``<arg>`` is the path for most ops but the *pattern* for grep/glob (``Grep 'foo|bar': …``), so a
# hint that is really about the path must not phrase itself as a claim about ``<arg>`` (e.g. "is not a
# path" reads false after a pattern). Phrase new hints to follow that prefix, not as standalone
# capitalised sentences.
_FS_CODE_HINTS: dict[FsErrorCode, str] = {
    FsErrorCode.INVALID_PATH: (
        "targets a path outside the accessible workspace — the file tools only reach /workspace (the "
        "repo, skills and tmp subtrees) and reject '..' segments; pass an absolute path under "
        "/workspace, or use the bash tool to inspect files elsewhere in the sandbox"
    ),
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


class _ReadFault(NamedTuple):
    """An unusable piece of sandbox read metadata. ``key`` dedupes the log across a run."""

    key: str
    level: int
    detail: str


_MISSING_READ_WINDOW = _ReadFault(
    "no-line-metadata", logging.ERROR, "returned no read-window metadata; deploy a matching daiv-sandbox release"
)


def _read_window_fault(end_line: int, content: str, offset: int, limit: int) -> _ReadFault | None:
    """Check the sandbox's reported read window against the request, or return ``None`` when it holds.

    Every fault drops the window and keeps the content: the model loses the pagination notice but
    still gets the page. Rebuilding the window from the returned text is never the fallback — a
    byte-capped page carries the sandbox's banner inline, so counting its rows resumes past unread
    source lines.
    """
    if end_line == offset:
        # The page's first line alone exceeds the byte cap, so it holds no complete line — and
        # deepagents cannot express a zero-line window.
        return _ReadFault("empty-page", logging.WARNING, "returned no complete line (byte cap)")
    if not offset < end_line <= offset + limit:
        return _ReadFault(
            "end-line-out-of-range", logging.ERROR, f"returned end_line={end_line} outside the requested window"
        )
    if not content:
        return _ReadFault(
            "window-over-empty-content", logging.ERROR, f"reported a window ending at {end_line} over empty content"
        )
    return None


class SandboxFileBackend(BackendProtocol):
    """Deepagents backend whose files live in a sandbox workspace, and the run's
    command-execution handle (``run_commands``).

    The agent addresses files by their sandbox-absolute path (``/workspace/repo``,
    ``/workspace/skills``, ``/workspace/tmp``); the backend is a thin proxy to
    ``DAIVSandboxClient`` — the sandbox is authoritative, so there is no local mirror.
    The only translation is in :meth:`_abs`, which maps the virtual root ``/`` (and the empty path)
    onto the workspace root; every other path is passed through verbatim for the sandbox to accept
    (when under ``/workspace``) or reject. Every op is one RPC over ``DAIVSandboxClient``; there is no
    local copy, so no rollback/desync machinery.

    The backend wraps the run's :class:`~automation.agent.workspace.session.SandboxSession` and reaches the container
    it holds. Any file op before ``SandboxMiddleware.abefore_agent`` acquires the session raises ``RuntimeError`` (a
    programming error — the middleware must acquire first).

    Only the async methods are implemented — the async agent path never calls the
    sync ones (the inherited sync methods raise ``NotImplementedError``; a sync call
    here would be a programming error). ``unlink`` and ``stat_mode`` round out
    ``DAIVBackendProtocol``; ``stat_mode`` returns a constant since the sandbox is
    authoritative (no mirror to a local repo), so exact mode bits are irrelevant.

    Note: ``awrite`` writes files at a fixed ``0o644`` (the file tools don't carry a mode), so
    an executable bit must be set via ``bash`` (``chmod +x``) in the sandbox, not through the
    file tools. ``aedit`` carries no mode (``FsEditRequest`` has no mode field), so the sandbox
    edits in place and leaves the existing file's mode untouched.
    """

    def __init__(self, session: SandboxSession) -> None:
        self._session = session
        self._logged_read_faults: set[str] = set()

    @property
    def session(self) -> SandboxSession:
        """The run's sandbox session, shared with the middleware that acquires it and the publisher that refreshes its
        credential."""
        return self._session

    def _require_bound(self) -> tuple[DAIVSandboxClient, str]:
        session_id = self._session.session_id
        if session_id is None:
            raise RuntimeError("SandboxFileBackend is not bound to a sandbox session")
        return self._session.client, session_id

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
    # The sandbox is authoritative and the agent addresses files by their sandbox-absolute path
    # (/workspace/repo, /workspace/skills, /workspace/tmp). The ONLY normalisation here is the
    # deepagents virtual root "/" (and the empty path) — the path-less glob/grep/ls default — onto
    # the workspace root, so those defaults search /workspace rather than being rejected. Every other
    # path passes straight through to the sandbox unchanged.
    #
    # We deliberately do NOT re-home an out-of-workspace path under the repo root. A repo slip (dropped
    # "/workspace/repo" prefix, e.g. "/daiv/foo") is indistinguishable from a path the model means
    # literally (an installed package under the sandbox home, "/home/daiv-sandbox/.local/.../dbt/impl.py"),
    # so re-homing the latter to a bogus "/workspace/repo/home/..." once reported a misleading "does not
    # exist" for a file that exists. Passing the path through lets the sandbox reject it with an honest
    # ``invalid_path`` instead of guessing — matching disk-backed runs, which never auto-corrected either.
    def _abs(self, backend_path: str) -> str:
        if not backend_path or backend_path == "/":
            return WORKSPACE_PATH
        return backend_path

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
        content = resp.content or ""
        encoding = resp.encoding or "utf-8"
        file_data = FileData(content=content, encoding=encoding)
        # Only line-windowed text reads carry a pagination window; binary (base64) reads the whole
        # file and the empty-file sentinel is not file content.
        if encoding == "base64" or content == EMPTY_CONTENT_WARNING:
            return ReadResult(file_data=file_data)

        if (end_line := resp.end_line) is None:
            self._log_window_fault(_MISSING_READ_WINDOW, file_path, offset, limit)
            return ReadResult(file_data=file_data)
        if fault := _read_window_fault(end_line, content, offset, limit):
            self._log_window_fault(fault, file_path, offset, limit)
            return ReadResult(file_data=file_data)

        total_lines = resp.total_lines
        if total_lines is not None and total_lines < end_line:
            # Degrade to an unknown total rather than drop the window: a resume offset still works.
            self._log_read_fault(
                "total-below-end-line",
                logging.ERROR,
                "Sandbox read of %r reported total_lines=%s below end_line=%s; dropping the total",
                file_path,
                total_lines,
                end_line,
            )
            total_lines = None
        try:
            return ReadResult(
                file_data=file_data,
                start_line=offset + 1,
                end_line=end_line,
                total_lines=total_lines,
                # An unknown total over-advertises a resume offset at EOF, which the next read rejects as
                # an invalid offset. A missing one would read as EOF and silently drop the rest of the file.
                next_offset=None if total_lines is not None and end_line >= total_lines else end_line,
            )
        except ValueError as exc:
            # ReadResult enforces more window invariants than the checks above mirror, and it enforces
            # them by raising — which would escape the tool and kill the run.
            self._log_read_fault(
                "rejected-read-window",
                logging.ERROR,
                "Sandbox read of %r built a window deepagents rejects (%s); dropping it",
                file_path,
                exc,
            )
            return ReadResult(file_data=file_data)

    def _log_read_fault(self, key: str, level: int, msg: str, *args) -> None:
        """Report unusable read metadata once per fault kind per run. Every cause is systematic — a
        version skew or a sandbox arithmetic slip repeats on every read — and each ERROR is a Sentry
        event, so repeating one buries every other breadcrumb in the event it eventually attaches to.
        """
        if key in self._logged_read_faults:
            return
        self._logged_read_faults.add(key)
        logger.log(level, msg, *args)

    def _log_window_fault(self, fault: _ReadFault, file_path: str, offset: int, limit: int) -> None:
        self._log_read_fault(
            fault.key,
            fault.level,
            "Sandbox read of %r %s (offset=%s limit=%s); dropping the pagination window",
            file_path,
            fault.detail,
            offset,
            limit,
        )

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        """Regex grep via the sandbox's ERE search.

        ``max_count`` is applied here rather than pushed down: ``FsGrepRequest`` carries no cap,
        so the sandbox always returns its full result and the trim happens on this side.
        ``context_lines`` is accepted for signature parity only — the RPC returns matching lines
        with no surrounding context, and no DAIV caller requests it. ``FsGrepRequest.exclude`` is
        likewise never set (nor by :meth:`aglob`), so both searches get only the sandbox's own
        default directory pruning.
        """
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
        capped = max_count is not None and len(matches) > max_count
        if capped:
            matches = matches[:max_count]
        if resp.truncated:
            logger.warning("grep results truncated for pattern %r under %s", pattern, path)
        # deepagents 0.7 renders its own truncation guidance from this flag, so the note no longer
        # has to be smuggled through a synthetic match's ``path`` to survive `files_with_matches`.
        return GrepResult(matches=matches, truncated=resp.truncated or capped)

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
    async def unlink(self, virtual_path: str) -> bool:
        client, session_id = self._require_bound()
        # ``unlink``'s protocol return is a bare bool with no error channel, so any failure — a
        # transport fault or a sandbox-reported reason — can only be reported as ``False``. Log it
        # first in both branches so a failed unlink is diagnosable rather than a silent ``False``.
        try:
            resp = await client.fs_delete(session_id, FsDeleteRequest(path=self._abs(virtual_path)))
        except httpx.HTTPError as exc:
            logger.warning("Sandbox unlink transport failure for %s: %s", virtual_path, exc)
            return False
        if resp.error is not None:
            logger.warning("Sandbox unlink failed for %s: %s", virtual_path, _fs_error_text(resp.error))
            return False
        if not resp.removed:
            # Idempotent success: the path was already absent. Match ``DAIVFilesystemBackend.unlink``
            # (``Path.unlink(missing_ok=True)``), which also reports success for a no-op removal.
            logger.debug("Sandbox unlink: %s was already absent (nothing removed)", virtual_path)
        return True

    async def stat_mode(self, virtual_path: str) -> int:
        return 0o644
