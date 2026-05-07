from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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

    Bundles the backend, the working_dir → /repo mapping, the sandbox client (owned and
    lifecycle-managed by ``SandboxMiddleware``), and a single lock that serialises the
    upstream-write→sandbox-sync critical section across all concurrent write_file/edit_file
    calls in one agent run, so rollback can never overwrite a sibling tool call's edit.
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

    def resolve_target(self, file_path: str) -> Path:
        """Re-run upstream's path validation + backend resolution to obtain the on-disk target."""
        return Path(self.backend._resolve_path(validate_path(file_path)))

    async def mirror(
        self, *, runtime, resolved_path: Path | str, content_bytes: bytes, mode: int, rollback: Callable[[], bool]
    ) -> str | None:
        """Mirror a successful local write/edit to the sandbox; rollback on failure.

        Returns an error string for the tool to surface, or ``None`` on success.
        """
        try:
            sandbox_path = self.sandbox_path(resolved_path)
        except ValueError as exc:
            return format_sync_error(f"failed to prepare sandbox sync: {exc}", rollback_ok=rollback())

        session_id = runtime.state.get("session_id") if runtime.state else None
        if not session_id:
            return format_sync_error("sandbox session not started", rollback_ok=rollback())

        request = ApplyMutationsRequest(
            mutations=[PutMutation(path=sandbox_path, content=base64.b64encode(content_bytes), mode=mode)]
        )
        try:
            response = await self.client.apply_file_mutations(session_id, request)
        except Exception as exc:
            logger.exception("sandbox apply_file_mutations failed for %s", sandbox_path)
            return format_sync_error(f"sandbox sync raised: {exc}", rollback_ok=rollback())

        result = response.results[0]
        if not result.ok:
            return format_sync_error(f"failed to sync to sandbox: {result.error}", rollback_ok=rollback())
        return None
