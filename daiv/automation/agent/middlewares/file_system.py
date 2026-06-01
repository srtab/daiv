from __future__ import annotations

import asyncio
import base64
import logging
import stat
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

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
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE

from core.sandbox.client import DAIVSandboxClient  # noqa: TC001
from core.sandbox.schemas import (
    FsDeleteRequest,
    FsEditRequest,
    FsGlobRequest,
    FsGrepRequest,
    FsLsRequest,
    FsReadRequest,
    FsWriteRequest,
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


GREP_TOOL_DESCRIPTION = _with_path_reminder(GREP_TOOL_DESCRIPTION_BASE)
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

FILESYSTEM_ABSOLUTE_PATH_DIRECTIVE = (
    'Filesystem tool-call arguments (ls/read_file/edit_file/etc.) MUST use absolute paths (start with "/").'
)

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


class SandboxFileBackend(BackendProtocol):
    """Deepagents backend whose files live in a sandbox workspace.

    The agent addresses files by their sandbox-absolute path (``/workspace/repo``,
    ``/workspace/skills``, ``/workspace/tmp``); the backend is a thin pass-through to
    ``DAIVSandboxClient`` — the sandbox is authoritative, so there is no path translation
    or local mirror. Every op is one RPC over ``DAIVSandboxClient``; there is no local
    copy, so no rollback/desync machinery.

    The backend is constructed at graph-build time (before the per-run session exists)
    and **bound** to a live client+session via :meth:`bind` once
    ``SandboxMiddleware.abefore_agent`` has started the session. Any file op before
    binding raises ``RuntimeError`` (a programming error — the middleware must bind first).

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

    def bind(self, client: DAIVSandboxClient, session_id: str) -> None:
        """Attach the live per-run client + session. Called once the sandbox session exists.

        The backend is tied to one **workspace**, identified by ``session_id``. Subagents share
        the parent's backend instance but each ``SandboxMiddleware`` opens its *own* client, so a
        subagent legitimately re-binds the *same* session through a *different* client — that is
        allowed (the new client just becomes the window onto the same session). Re-binding to a
        *different* session is a programming error and raises, rather than silently redirecting
        every file op to another workspace.
        """
        if self._session_id is not None and self._session_id != session_id:
            raise RuntimeError(
                f"SandboxFileBackend is already bound to session {self._session_id!r}; "
                f"refusing to rebind to {session_id!r}"
            )
        self._client = client
        self._session_id = session_id

    def _require_bound(self) -> tuple[DAIVSandboxClient, str]:
        if self._client is None or not self._session_id:
            raise RuntimeError("SandboxFileBackend is not bound to a sandbox session")
        return self._client, self._session_id

    # -- path mapping (identity) --------------------------------------------
    # The sandbox is authoritative and the agent addresses files by their
    # sandbox-absolute path (/workspace/repo, /workspace/skills, /workspace/tmp).
    # There is no translation: paths pass straight through. Kept as helpers so the
    # async methods below need no change and an empty path normalises to "/".
    def _abs(self, backend_path: str) -> str:
        return backend_path or "/"

    def _rel(self, abs_path: str) -> str:
        return abs_path or "/"

    # -- async protocol methods ---------------------------------------------
    # The Fs*Response list types carry an ``error`` field (populated, with an empty list,
    # on a soft sandbox failure returned as 200). Propagate it into the deepagents result's
    # ``error`` so the filesystem middleware surfaces it to the model — otherwise a real
    # failure reads as a clean "empty directory / no matches".
    async def als(self, path: str) -> LsResult:
        client, session_id = self._require_bound()
        resp = await client.fs_ls(session_id, FsLsRequest(path=self._abs(path)))
        if resp.error is not None:
            return LsResult(error=f"Listing '{path}': {resp.error}")
        return LsResult(entries=[FileInfo(path=self._rel(e.path), is_dir=e.is_dir) for e in resp.entries])

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        client, session_id = self._require_bound()
        resp = await client.fs_read(session_id, FsReadRequest(path=self._abs(file_path), offset=offset, limit=limit))
        if resp.error is not None:
            return ReadResult(error=f"File '{file_path}': {resp.error}")
        return ReadResult(file_data=FileData(content=resp.content or "", encoding=resp.encoding or "utf-8"))

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        client, session_id = self._require_bound()
        resp = await client.fs_grep(session_id, FsGrepRequest(pattern=pattern, path=self._abs(path or "/"), glob=glob))
        if resp.error is not None:
            return GrepResult(error=f"Grep '{pattern}': {resp.error}")
        return GrepResult(matches=[GrepMatch(path=self._rel(m.path), line=m.line, text=m.text) for m in resp.matches])

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        client, session_id = self._require_bound()
        resp = await client.fs_glob(session_id, FsGlobRequest(pattern=pattern, path=self._abs(path)))
        if resp.error is not None:
            return GlobResult(error=f"Glob '{pattern}': {resp.error}")
        return GlobResult(matches=[FileInfo(path=self._rel(e.path), is_dir=e.is_dir) for e in resp.matches])

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        client, session_id = self._require_bound()
        resp = await client.fs_write(
            session_id,
            FsWriteRequest(path=self._abs(file_path), content=base64.b64encode(content.encode("utf-8")), mode=0o644),
        )
        if not resp.ok:
            return WriteResult(error=f"Failed to write file '{file_path}': {resp.error or 'unknown sandbox error'}")
        return WriteResult(path=file_path)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        client, session_id = self._require_bound()
        resp = await client.fs_edit(
            session_id,
            FsEditRequest(path=self._abs(file_path), old=old_string, new=new_string, replace_all=replace_all),
        )
        if resp.error is not None:
            return EditResult(error=f"Error editing file '{file_path}': {resp.error}")
        return EditResult(path=file_path, occurrences=resp.occurrences if resp.occurrences is not None else 1)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        client, session_id = self._require_bound()
        out: list[FileUploadResponse] = []
        for path, data in files:
            resp = await client.fs_write(
                session_id, FsWriteRequest(path=self._abs(path), content=base64.b64encode(data), mode=0o644)
            )
            # A failed write with no error string would otherwise be reported as success
            # (error=None); fall back to a generic message so ok=False always carries one.
            error = None if resp.ok else (resp.error or "unknown sandbox error")
            # deepagents annotates ``error`` as the narrow ``FileOperationError`` literal but
            # documents accepting backend-specific strings; the sandbox returns its own messages.
            out.append(FileUploadResponse(path=path, error=error))  # ty: ignore[invalid-argument-type]
        return out

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        client, session_id = self._require_bound()
        out: list[FileDownloadResponse] = []
        for path in paths:
            resp = await client.fs_read(session_id, FsReadRequest(path=self._abs(path)))
            if resp.error == FILE_NOT_FOUND:
                out.append(FileDownloadResponse(path=path, error=FILE_NOT_FOUND))
            elif resp.error is not None:
                # See ``aupload_files``: deepagents accepts backend-specific error strings.
                out.append(
                    FileDownloadResponse(path=path, error=resp.error)  # ty: ignore[invalid-argument-type]
                )
            elif resp.encoding == "base64":
                out.append(FileDownloadResponse(path=path, content=base64.b64decode(resp.content or "")))
            else:
                out.append(FileDownloadResponse(path=path, content=(resp.content or "").encode("utf-8")))
        return out

    # -- DAIVBackendProtocol -------------------------------------------------
    async def delete(self, virtual_path: str) -> bool:
        client, session_id = self._require_bound()
        resp = await client.fs_delete(session_id, FsDeleteRequest(path=self._abs(virtual_path)))
        if not resp.ok:
            # The protocol return is a bare bool, so the sandbox's reason would otherwise be lost;
            # log it so a failed delete is diagnosable rather than a silent ``False``.
            logger.warning("Sandbox delete failed for %s: %s", virtual_path, resp.error or "unknown sandbox error")
        return resp.ok

    async def stat_mode(self, virtual_path: str) -> int:
        return 0o644
