from __future__ import annotations

import asyncio
import base64
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import FilesystemState
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.tools import BaseTool, StructuredTool

from core.sandbox.client import DAIVSandboxClient  # noqa: TC001
from core.sandbox.schemas import ApplyMutationsRequest, PutMutation

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _format_sync_error(reason: str, *, rollback_ok: bool) -> str:
    if rollback_ok:
        return f"Error: {reason}"
    return f"CRITICAL: {reason}; rollback also failed — local state is desynced from sandbox"


@dataclass
class SandboxSyncer:
    """Mirrors successful local writes to the sandbox session associated with the agent run.

    Bundles the backend, the working_dir → /repo mapping, the sandbox client (owned and
    lifecycle-managed by ``SandboxMiddleware``), and a single lock that serialises the
    upstream-write→sandbox-sync critical section across all concurrent write_file/edit_file
    calls in one agent run, so rollback can never overwrite a sibling tool call's edit.
    The edit-path snapshot is taken outside the lock — if it fails we short-circuit to
    upstream's canonical "not found" error rather than acquiring the lock for a doomed call.
    """

    backend: Any
    working_dir: Path
    client: DAIVSandboxClient
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _resolved_working_dir: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._resolved_working_dir = self.working_dir.resolve()

    def sandbox_path(self, local_path: str | Path) -> str:
        """Map ``<working_dir>/<rel>`` → ``/repo/<rel>``. Raises ``ValueError`` if outside ``working_dir``."""
        rel = Path(local_path).resolve().relative_to(self._resolved_working_dir)
        return str(Path(SANDBOX_PATH_ROOT) / rel)

    async def mirror(
        self, *, runtime, resolved_path: Path | str, content_bytes: bytes, mode: int, rollback: Callable[[], bool]
    ) -> str | None:
        """Mirror a successful local write/edit to the sandbox; rollback on failure.

        Returns an error string for the tool to surface, or ``None`` on success.
        """
        try:
            sandbox_path = self.sandbox_path(resolved_path)
        except ValueError as exc:
            return _format_sync_error(f"failed to prepare sandbox sync: {exc}", rollback_ok=rollback())

        session_id = runtime.state.get("session_id") if runtime.state else None
        if not session_id:
            return _format_sync_error("sandbox session not started", rollback_ok=rollback())

        request = ApplyMutationsRequest(
            mutations=[PutMutation(path=sandbox_path, content=base64.b64encode(content_bytes), mode=mode)]
        )
        try:
            response = await self.client.apply_file_mutations(session_id, request)
        except Exception as exc:
            logger.exception("sandbox apply_file_mutations failed for %s", sandbox_path)
            return _format_sync_error(f"sandbox sync raised: {exc}", rollback_ok=rollback())

        result = response.results[0]
        if not result.ok:
            return _format_sync_error(f"failed to sync to sandbox: {result.error}", rollback_ok=rollback())
        return None

    def _resolve_target(self, file_path: str) -> Path:
        """Re-run upstream's path validation + backend resolution to obtain the on-disk target."""
        return Path(self.backend._resolve_path(validate_path(file_path)))

    def wrap_write_tool(self, original: BaseTool) -> StructuredTool:
        """Wrap an upstream ``write_file`` tool so each successful write is mirrored to the sandbox."""
        syncer = self
        original_coroutine = _require_coroutine(original)

        async def wrapped(
            file_path: Annotated[
                str, "Absolute path where the file should be created. Must be absolute, not relative."
            ],
            content: Annotated[str, "The text content to write to the file. This parameter is required."],
            runtime: ToolRuntime[None, FilesystemState],
        ) -> str:
            async with syncer.lock:
                upstream_result = await original_coroutine(file_path=file_path, content=content, runtime=runtime)
                if not upstream_result.startswith("Updated file"):
                    return upstream_result

                target = syncer._resolve_target(file_path)

                def _rollback() -> bool:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("rollback unlink failed for %s", target)
                        return False
                    return True

                try:
                    mode = stat.S_IMODE(target.stat().st_mode)
                except OSError as exc:
                    return _format_sync_error(f"failed to prepare sandbox sync: {exc}", rollback_ok=_rollback())

                error = await syncer.mirror(
                    runtime=runtime, resolved_path=target, content_bytes=content.encode(), mode=mode, rollback=_rollback
                )
                return error or upstream_result

        return StructuredTool.from_function(
            name=original.name, description=original.description, coroutine=wrapped, infer_schema=True
        )

    def wrap_edit_tool(self, original: BaseTool) -> StructuredTool:
        """Wrap an upstream ``edit_file`` tool so each successful edit is mirrored to the sandbox."""
        syncer = self
        original_coroutine = _require_coroutine(original)

        async def wrapped(
            file_path: Annotated[str, "Absolute path to the file to edit. Must be absolute, not relative."],
            old_string: Annotated[
                str, "The exact text to find and replace. Must be unique in the file unless replace_all is True."
            ],
            new_string: Annotated[str, "The text to replace old_string with. Must be different from old_string."],
            runtime: ToolRuntime[None, FilesystemState],
            *,
            replace_all: Annotated[
                bool, "If True, replace all occurrences of old_string. If False (default), old_string must be unique."
            ] = False,
        ) -> str:
            # Snapshot must happen before delegating, so we resolve + read the target up front.
            # If anything fails here (invalid path, missing file), let upstream produce the
            # canonical error instead of inventing our own.
            try:
                target = syncer._resolve_target(file_path)
                pre_bytes = target.read_bytes()
                pre_mode = stat.S_IMODE(target.stat().st_mode)
            except ValueError, OSError:
                return await original_coroutine(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    runtime=runtime,
                    replace_all=replace_all,
                )

            async with syncer.lock:
                upstream_result = await original_coroutine(
                    file_path=file_path,
                    old_string=old_string,
                    new_string=new_string,
                    runtime=runtime,
                    replace_all=replace_all,
                )
                if not upstream_result.startswith("Successfully replaced"):
                    return upstream_result

                def _rollback() -> bool:
                    try:
                        target.write_bytes(pre_bytes)
                        os.chmod(target, pre_mode)  # noqa: PTH101
                    except OSError:
                        logger.exception("rollback restore failed for %s", target)
                        return False
                    return True

                try:
                    post_bytes = target.read_bytes()
                except OSError as exc:
                    return _format_sync_error(f"failed to prepare sandbox sync: {exc}", rollback_ok=_rollback())

                error = await syncer.mirror(
                    runtime=runtime, resolved_path=target, content_bytes=post_bytes, mode=pre_mode, rollback=_rollback
                )
                return error or upstream_result

        return StructuredTool.from_function(
            name=original.name, description=original.description, coroutine=wrapped, infer_schema=True
        )


def _require_coroutine(tool: BaseTool):
    if tool.coroutine is None:
        raise TypeError(f"upstream tool {tool.name!r} has no async coroutine to wrap")
    return tool.coroutine
