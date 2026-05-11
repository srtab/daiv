from __future__ import annotations

import asyncio
import base64
import logging
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.store import StoreBackend
from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE

from core.sandbox.client import DAIVSandboxClient  # noqa: TC001
from core.sandbox.schemas import ApplyMutationsRequest, PutMutation

logger = logging.getLogger("daiv.tools")

SANDBOX_PATH_ROOT = "/repo"

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

REMINDER_ABSOLUTE_PATHS = """
IMPORTANT:
- Tool inputs/outputs use absolute paths (e.g. /repo/...).
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
# Sandbox sync
# ---------------------------------------------------------------------------


def format_sync_error(reason: str, *, rollback_ok: bool) -> str:
    if rollback_ok:
        return f"Error: {reason}"
    return f"CRITICAL: {reason}; rollback also failed — local state is desynced from sandbox"


@dataclass
class SandboxSyncer:
    """Mirrors successful local writes to the sandbox session associated with the agent run.

    Bundles the backend, the agent virtual root → ``/repo`` mapping, the sandbox client
    (owned and lifecycle-managed by ``SandboxMiddleware``), and a single lock that
    serialises the upstream-write→sandbox-sync critical section across all concurrent
    write_file/edit_file calls in one agent run, so rollback can never overwrite a
    sibling tool call's edit.

    ``agent_root`` is the virtual path prefix the agent's filesystem tools see (e.g.
    ``/repo``). Path mapping is pure string arithmetic, so the syncer works for both
    disk-backed (``FilesystemBackend``) and store-backed (``StoreBackend``) backends.
    """

    backend: Any
    agent_root: str
    client: DAIVSandboxClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def sandbox_path(self, virtual_path: str) -> str:
        """Map agent ``<agent_root>/<rel>`` → sandbox ``/repo/<rel>``.

        Raises ``ValueError`` if ``virtual_path`` is not under ``agent_root``.
        """
        prefix = self.agent_root.rstrip("/") + "/"
        normalized = validate_path(virtual_path, allowed_prefixes=[prefix])
        rel = normalized[len(prefix) :]
        return f"{SANDBOX_PATH_ROOT.rstrip('/')}/{rel}"

    async def mirror(
        self, *, runtime, virtual_path: str, content_bytes: bytes, mode: int, rollback: Callable[[], Awaitable[bool]]
    ) -> str | None:
        """Mirror a successful local write/edit to the sandbox; rollback on failure.

        Returns an error string for the tool to surface, or ``None`` on success. ``rollback``
        is an async callable returning whether the local-state restore succeeded; awaited
        only when the sandbox sync fails.
        """
        try:
            sandbox_path = self.sandbox_path(virtual_path)
        except ValueError as exc:
            return format_sync_error(f"failed to prepare sandbox sync: {exc}", rollback_ok=await rollback())

        session_id = runtime.state.get("session_id") if runtime.state else None
        if not session_id:
            return format_sync_error("sandbox session not started", rollback_ok=await rollback())

        request = ApplyMutationsRequest(
            mutations=[PutMutation(path=sandbox_path, content=base64.b64encode(content_bytes), mode=mode)]
        )
        try:
            response = await self.client.apply_file_mutations(session_id, request)
        except Exception as exc:
            logger.exception("sandbox apply_file_mutations failed for %s", sandbox_path)
            return format_sync_error(f"sandbox sync raised: {exc}", rollback_ok=await rollback())

        result = response.results[0]
        if not result.ok:
            return format_sync_error(f"failed to sync to sandbox: {result.error}", rollback_ok=await rollback())
        return None


# ---------------------------------------------------------------------------
# Backends
#
# Thin daiv-side extensions to deepagents' backends. Two methods that
# ``BackendProtocol`` doesn't expose:
#
# - ``delete(path)``: needed for write rollback (drop the just-created file when
#   sandbox sync fails). Disk uses ``Path.unlink``; the store calls
#   ``BaseStore.adelete`` directly via ``StoreBackend``'s own ``_get_store``/
#   ``_get_namespace`` (underscore-prefixed but stable inherited methods).
# - ``stat_mode(path)``: needed for the OUTGOING sandbox sync — ``PutMutation.mode``
#   carries POSIX mode bits so the sandbox replicates ``+x`` on executable scripts.
#   Disk reads real mode bits; the store has no mode concept and returns 0o644.
#
# Adding a new backend = subclass it + provide these two methods (plus an
# ``isinstance`` branch in ``sandbox._apply_patch_to_backend`` for the patch-apply
# dispatch and the gitignore guard, which need backend-shape, not just the methods).
# ---------------------------------------------------------------------------


class DAIVFilesystemBackend(FilesystemBackend):
    """``FilesystemBackend`` with sandbox-sync hooks (``delete`` for rollback,
    ``stat_mode`` to mirror real POSIX mode bits onto ``PutMutation``)."""

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


class DAIVStoreBackend(StoreBackend):
    """``StoreBackend`` with sandbox-sync hooks. The store has no mode concept;
    ``stat_mode`` always returns 0o644. ``delete`` calls ``BaseStore.adelete``
    directly — ``BackendProtocol`` has no public delete.
    """

    async def delete(self, virtual_path: str) -> bool:
        try:
            store = cast("Any", self)._get_store()
            namespace = cast("Any", self)._get_namespace()
            await store.adelete(namespace, virtual_path)
        except Exception:
            logger.exception("store delete failed for %s", virtual_path)
            return False
        return True

    async def stat_mode(self, virtual_path: str) -> int:  # noqa: ARG002
        return 0o644
