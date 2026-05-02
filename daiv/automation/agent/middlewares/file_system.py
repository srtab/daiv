from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path
from typing import Annotated

from deepagents.backends.utils import validate_path
from deepagents.middleware.filesystem import EDIT_FILE_TOOL_DESCRIPTION as EDIT_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GLOB_TOOL_DESCRIPTION as GLOB_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import GREP_TOOL_DESCRIPTION as GREP_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import LIST_FILES_TOOL_DESCRIPTION as LIST_FILES_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import READ_FILE_TOOL_DESCRIPTION as READ_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import WRITE_FILE_TOOL_DESCRIPTION as WRITE_FILE_TOOL_DESCRIPTION_BASE
from deepagents.middleware.filesystem import FilesystemMiddleware as BaseFilesystemMiddleware
from langchain_core.prompts import SystemMessagePromptTemplate
from langchain_core.tools import StructuredTool

from core.sandbox.client import DAIVSandboxClient
from core.sandbox.schemas import ApplyMutationsRequest, PutMutation

logger = logging.getLogger("daiv.tools")

REMINDER_ABSOLUTE_PATHS = """
IMPORTANT:
- Tool inputs/outputs use absolute paths (e.g. /repo/...).
- DO NOT output these absolute paths to the user.
- Convert to repo-relative paths in all user-visible text.
"""

GREP_TOOL_DESCRIPTION = GREP_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

GLOB_TOOL_DESCRIPTION = GLOB_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

LIST_FILES_TOOL_DESCRIPTION = LIST_FILES_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

READ_FILE_TOOL_DESCRIPTION = READ_FILE_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

# The agent is calling write_file on a file that already exists, gets rejected
# ("Cannot write because it already exists"), then correctly switches to edit_file. This wastes one tool call.
WRITE_FILE_TOOL_DESCRIPTION = (
    WRITE_FILE_TOOL_DESCRIPTION_BASE
    + "\nIMPORTANT: This tool can ONLY create new files. It will fail on files that already exist. "
    "To modify existing files, always use `edit_file` instead." + "\n" + REMINDER_ABSOLUTE_PATHS
)

EDIT_FILE_TOOL_DESCRIPTION = EDIT_FILE_TOOL_DESCRIPTION_BASE + "\n" + REMINDER_ABSOLUTE_PATHS

DAIV_FILESYSTEM_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    """\
## Filesystem Tools

You have access to a filesystem which you can interact with using these tools.
Tool-call arguments (ls/read_file{{^read_only}}/edit_file{{/read_only}}/etc.) MUST use absolute paths (start with "/").
User-visible output MUST NEVER contain "/repo/" and MUST use repo-relative paths (e.g. daiv/core/utils.py).""",
    "mustache",
)

SANDBOX_PATH_ROOT = "/repo"


class FilesystemMiddleware(BaseFilesystemMiddleware):
    """DAIV's FilesystemMiddleware customisation.

    Adds:
    - Custom tool descriptions referencing absolute paths.
    - A `read_only` mode that strips the write/edit tools.
    - When `sandbox_sync=True`, replaces `write_file` and `edit_file` with
      sync-aware versions that mirror successful local writes to the
      daiv-sandbox session named by `state["session_id"]`. Reads still go
      against local disk for speed; bash-induced changes flow back via the
      patch extractor's per-turn diff.

    Args:
        backend: Backend for file storage. Should be a deepagents `FilesystemBackend`.
        read_only: When True, removes write_file and edit_file from the exposed tools.
        sandbox_sync: When True, replace write_file/edit_file with sync-aware versions.
        working_dir: Required when sandbox_sync=True. The on-disk repo root used to
            map local paths to sandbox paths (`/<rel>` from `<working_dir>` →
            `/repo/<rel>`).
        sandbox_client_factory: Optional override of the DAIVSandboxClient factory.
            Useful for tests.

    Example:
        ```python
        from deepagents.backends.filesystem import FilesystemBackend
        from automation.agent.middlewares.file_system import FilesystemMiddleware

        backend = FilesystemBackend(root_dir="/workspace", virtual_mode=True)
        middleware = FilesystemMiddleware(backend=backend, sandbox_sync=True, working_dir="/workspace/repo")
        ```
    """

    def __init__(
        self,
        *args,
        read_only: bool = False,
        sandbox_sync: bool = False,
        working_dir: Path | str | None = None,
        sandbox_client_factory=None,
        **kwargs,
    ) -> None:
        system_prompt = kwargs.pop("system_prompt", DAIV_FILESYSTEM_SYSTEM_PROMPT.format(read_only=read_only).content)
        custom_tool_descriptions = kwargs.pop(
            "custom_tool_descriptions",
            {
                "grep": GREP_TOOL_DESCRIPTION,
                "glob": GLOB_TOOL_DESCRIPTION,
                "ls": LIST_FILES_TOOL_DESCRIPTION,
                "read_file": READ_FILE_TOOL_DESCRIPTION,
                "write_file": WRITE_FILE_TOOL_DESCRIPTION,
                "edit_file": EDIT_FILE_TOOL_DESCRIPTION,
            },
        )
        super().__init__(
            *args, system_prompt=system_prompt, custom_tool_descriptions=custom_tool_descriptions, **kwargs
        )
        excluded_tools = ["execute"]
        if read_only:
            excluded_tools.append("edit_file")
            excluded_tools.append("write_file")
        self.tools = [tool for tool in self.tools if tool.name not in excluded_tools]

        if sandbox_sync and not read_only:
            if working_dir is None:
                raise ValueError("sandbox_sync=True requires working_dir to be provided")
            backend = kwargs.get("backend") or (args[0] if args else None)
            if backend is None:
                raise ValueError("sandbox_sync=True requires the backend to be provided")
            # Resolve the factory at construction time so test monkeypatching of
            # `DAIVSandboxClient` on this module sticks (default args are captured
            # at function-definition time, which would defeat the monkeypatch).
            if sandbox_client_factory is None:
                sandbox_client_factory = DAIVSandboxClient
            self._install_sync_wrappers(
                backend=backend, working_dir=Path(working_dir), sandbox_client_factory=sandbox_client_factory
            )

    def _install_sync_wrappers(self, *, backend, working_dir: Path, sandbox_client_factory) -> None:
        """Replace write_file (and edit_file once Task 17 lands) with sync-aware versions."""
        new_tools = []
        for tool in self.tools:
            if tool.name == "write_file":
                new_tools.append(_make_sync_write_tool(backend, working_dir, sandbox_client_factory))
            elif tool.name == "edit_file" and _make_sync_edit_tool is not None:
                new_tools.append(_make_sync_edit_tool(backend, working_dir, sandbox_client_factory))
            else:
                new_tools.append(tool)
        self.tools = new_tools


def _sandbox_path_for(local_path: str | Path, working_dir: Path) -> str:
    """Map <working_dir>/<rel> → /repo/<rel>. Raises ValueError if outside working_dir."""
    rel = Path(local_path).resolve().relative_to(Path(working_dir).resolve())
    return str(Path(SANDBOX_PATH_ROOT) / rel)


async def _sync_put(client, session_id: str, sandbox_path: str, content: bytes, mode: int) -> tuple[bool, str | None]:
    """POST a single put. Returns (ok, error_message)."""
    request = ApplyMutationsRequest(
        mutations=[PutMutation(path=sandbox_path, content=base64.b64encode(content), mode=mode)]
    )
    try:
        response = await client.apply_file_mutations(session_id, request)
    except Exception as exc:
        return False, f"sandbox sync raised: {exc}"
    result = response.results[0]
    return result.ok, result.error


def _rollback_write(path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.exception("rollback unlink failed for %s", path)


def _make_sync_write_tool(backend, working_dir: Path, sandbox_client_factory) -> StructuredTool:
    """A sync-aware replacement for deepagents' write_file."""

    async def coroutine(
        file_path: Annotated[str, "Absolute path where the file should be created. Must be absolute, not relative."],
        content: Annotated[str, "The text content to write to the file. This parameter is required."],
        runtime,
    ) -> str:
        try:
            validated_path = validate_path(file_path)
        except ValueError as e:
            return f"Error: {e}"

        # Resolve to a real on-disk path so we can read mode/bytes after the write.
        resolved_path = backend._resolve_path(validated_path)

        result = await backend.awrite(validated_path, content)
        if result.error:
            return result.error

        try:
            sandbox_path = _sandbox_path_for(resolved_path, working_dir)
            content_bytes = Path(resolved_path).read_bytes()
            mode = stat.S_IMODE(Path(resolved_path).stat().st_mode)
        except (ValueError, OSError) as exc:
            _rollback_write(resolved_path)
            return f"Error: failed to prepare sandbox sync for {file_path}: {exc}"

        session_id = (runtime.state or {}).get("session_id")
        if not session_id:
            _rollback_write(resolved_path)
            return "Error: sandbox session not started"

        client = sandbox_client_factory()
        ok, err = await _sync_put(client, session_id, sandbox_path, content_bytes, mode)
        if not ok:
            _rollback_write(resolved_path)
            return f"Error: failed to sync write to sandbox: {err}"

        return f"Updated file {result.path}"

    return StructuredTool.from_function(
        name="write_file", description=WRITE_FILE_TOOL_DESCRIPTION, coroutine=coroutine, infer_schema=True
    )


def _make_sync_edit_tool(backend, working_dir: Path, sandbox_client_factory) -> StructuredTool:
    """A sync-aware replacement for deepagents' edit_file."""

    async def coroutine(
        file_path: Annotated[str, "Absolute path to the file to edit. Must be absolute, not relative."],
        old_string: Annotated[
            str, "The exact text to find and replace. Must be unique in the file unless replace_all is True."
        ],
        new_string: Annotated[str, "The text to replace old_string with. Must be different from old_string."],
        runtime,
        *,
        replace_all: Annotated[
            bool, "If True, replace all occurrences of old_string. If False (default), old_string must be unique."
        ] = False,
    ) -> str:
        try:
            validated_path = validate_path(file_path)
        except ValueError as e:
            return f"Error: {e}"

        resolved_path = backend._resolve_path(validated_path)

        # Snapshot pre-edit bytes + mode for rollback.
        try:
            pre_bytes = Path(resolved_path).read_bytes()
            pre_mode = stat.S_IMODE(Path(resolved_path).stat().st_mode)
        except OSError as exc:
            return f"Error: cannot read {file_path} before edit: {exc}"

        result = await backend.aedit(validated_path, old_string, new_string, replace_all=replace_all)
        if result.error:
            return result.error

        def _rollback() -> None:
            try:
                Path(resolved_path).write_bytes(pre_bytes)
                os.chmod(resolved_path, pre_mode)  # noqa: PTH101
            except OSError:
                logger.exception("rollback restore failed for %s", resolved_path)

        try:
            sandbox_path = _sandbox_path_for(resolved_path, working_dir)
            post_bytes = Path(resolved_path).read_bytes()
            post_mode = stat.S_IMODE(Path(resolved_path).stat().st_mode)
        except (ValueError, OSError) as exc:
            _rollback()
            return f"Error: failed to prepare sandbox sync for {file_path}: {exc}"

        session_id = (runtime.state or {}).get("session_id")
        if not session_id:
            _rollback()
            return "Error: sandbox session not started"

        client = sandbox_client_factory()
        ok, err = await _sync_put(client, session_id, sandbox_path, post_bytes, post_mode)
        if not ok:
            _rollback()
            return f"Error: failed to sync edit to sandbox: {err}"

        return f"Successfully replaced {result.occurrences} instance(s) of the string in '{result.path}'"

    return StructuredTool.from_function(
        name="edit_file", description=EDIT_FILE_TOOL_DESCRIPTION, coroutine=coroutine, infer_schema=True
    )
