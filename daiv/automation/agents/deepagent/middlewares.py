from __future__ import annotations

from typing import TYPE_CHECKING

from deepagents.middleware.filesystem import FilesystemMiddleware as BaseFilesystemMiddleware
from deepagents.middleware.filesystem import FilesystemState, _get_backend, _validate_path
from langchain.tools import ToolRuntime  # noqa: TC002
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import Command

if TYPE_CHECKING:
    from collections.abc import Callable

    from deepagents.backends.protocol import BackendProtocol

    from .backends import DeleteResult, RenameResult


DELETE_TOOL_DESCRIPTION = """Deletes a file or directory from the filesystem.

Usage:
- The path parameter must be an absolute path, not a relative path
- For FILES: Deletes the specified file
- For DIRECTORIES: Must set recursive=True to delete a directory and all its contents
- This operation is irreversible - exercise caution to avoid unintended data loss
- Before deleting a directory, examine its contents with the ls or glob tool

Examples:
- delete(path="/file.txt") - Delete a single file
- delete(path="/empty_dir", recursive=True) - Delete an empty directory
- delete(path="/project/old_code", recursive=True) - Delete directory and all contents"""

RENAME_TOOL_DESCRIPTION = """Renames a file or directory in the filesystem.

Usage:
- The path and new_path parameters must be absolute paths, not relative paths
- Works for both files and directories
- Errors if the old path doesn't exist
- Errors if the new path already exists (no overwrite)
- Creates parent directories for the new path if needed

Examples:
- rename(path="/old.txt", new_path="/new.txt") - Rename a file
- rename(path="/old_dir", new_path="/new_dir") - Rename a directory
- rename(path="/src/old.py", new_path="/lib/new.py") - Move and rename"""


DAIV_FILESYSTEM_SYSTEM_PROMPT = """## Filesystem Tools `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `delete`, `rename`

You have access to a filesystem which you can interact with using these tools.
All file paths must start with a /.

- ls: list files in a directory (requires absolute path)
- read_file: read a file from the filesystem
- write_file: write to a file in the filesystem
- edit_file: edit a file in the filesystem
- glob: find files matching a pattern (e.g., "**/*.py")
- grep: search for text within files
- delete: delete a file or directory (use recursive=True for directories)
- rename: rename or move a file or directory"""  # noqa: E501


def _delete_tool_generator(
    backend: BackendProtocol | Callable[[ToolRuntime], BackendProtocol], custom_description: str | None = None
) -> BaseTool:
    """Generate the delete tool.

    Args:
        backend: Backend to use for file storage, or a factory function.
        custom_description: Optional custom description for the tool.

    Returns:
        Configured delete tool that deletes files/directories using the backend.
    """
    tool_description = custom_description or DELETE_TOOL_DESCRIPTION

    def sync_delete(path: str, runtime: ToolRuntime[None, FilesystemState], recursive: bool = False) -> Command | str:
        """Synchronous wrapper for delete tool."""
        resolved_backend = _get_backend(backend, runtime)

        try:
            validated_path = _validate_path(path)
        except ValueError as e:
            return f"Error: {e}"

        res: DeleteResult = resolved_backend.delete(validated_path, recursive=recursive)

        if res.error:
            return res.error

        if res.files_update is not None:
            return Command(
                update={
                    "files": res.files_update,
                    "messages": [
                        ToolMessage(
                            content=f"Deleted {'directory' if recursive else 'file'} {res.path}",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )
        return f"Deleted {'directory' if recursive else 'file'} {res.path}"

    async def async_delete(
        path: str, runtime: ToolRuntime[None, FilesystemState], recursive: bool = False
    ) -> Command | str:
        """Asynchronous wrapper for delete tool."""
        resolved_backend = _get_backend(backend, runtime)

        try:
            validated_path = _validate_path(path)
        except ValueError as e:
            return f"Error: {e}"

        res: DeleteResult = await resolved_backend.adelete(validated_path, recursive=recursive)

        if res.error:
            return res.error

        if res.files_update is not None:
            return Command(
                update={
                    "files": res.files_update,
                    "messages": [
                        ToolMessage(
                            content=f"Deleted {'directory' if recursive else 'file'} {res.path}",
                            tool_call_id=runtime.tool_call_id,
                        )
                    ],
                }
            )
        return f"Deleted {'directory' if recursive else 'file'} {res.path}"

    return StructuredTool.from_function(
        name="delete", description=tool_description, func=sync_delete, coroutine=async_delete
    )


def _rename_tool_generator(
    backend: BackendProtocol | Callable[[ToolRuntime], BackendProtocol], custom_description: str | None = None
) -> BaseTool:
    """Generate the rename tool.

    Args:
        backend: Backend to use for file storage, or a factory function.
        custom_description: Optional custom description for the tool.

    Returns:
        Configured rename tool that renames files/directories using the backend.
    """
    tool_description = custom_description or RENAME_TOOL_DESCRIPTION

    def sync_rename(path: str, new_path: str, runtime: ToolRuntime[None, FilesystemState]) -> Command | str:
        """Synchronous wrapper for rename tool."""
        resolved_backend = _get_backend(backend, runtime)

        try:
            validated_old_path = _validate_path(path)
            validated_new_path = _validate_path(new_path)
        except ValueError as e:
            return f"Error: {e}"

        res: RenameResult = resolved_backend.rename(validated_old_path, validated_new_path)

        if res.error:
            return res.error

        if res.files_update is not None:
            return Command(
                update={
                    "files": res.files_update,
                    "messages": [
                        ToolMessage(
                            content=f"Renamed {res.old_path} to {res.new_path}", tool_call_id=runtime.tool_call_id
                        )
                    ],
                }
            )
        return f"Renamed {res.old_path} to {res.new_path}"

    async def async_rename(path: str, new_path: str, runtime: ToolRuntime[None, FilesystemState]) -> Command | str:
        """Asynchronous wrapper for rename tool."""
        resolved_backend = _get_backend(backend, runtime)

        try:
            validated_old_path = _validate_path(path)
            validated_new_path = _validate_path(new_path)
        except ValueError as e:
            return f"Error: {e}"

        res: RenameResult = await resolved_backend.arename(validated_old_path, validated_new_path)

        if res.error:
            return res.error

        if res.files_update is not None:
            return Command(
                update={
                    "files": res.files_update,
                    "messages": [
                        ToolMessage(
                            content=f"Renamed {res.old_path} to {res.new_path}", tool_call_id=runtime.tool_call_id
                        )
                    ],
                }
            )
        return f"Renamed {res.old_path} to {res.new_path}"

    return StructuredTool.from_function(
        name="rename", description=tool_description, func=sync_rename, coroutine=async_rename
    )


class FilesystemMiddleware(BaseFilesystemMiddleware):
    """Extended FilesystemMiddleware with delete and rename tools.

    This middleware extends the standard FilesystemMiddleware to add delete
    and rename capabilities for the DAIV deep agent.

    Args:
        backend: Backend for file storage. Should be DAIVFilesystemBackend or DAIVStateBackend.
        system_prompt: Optional custom system prompt override.
        custom_tool_descriptions: Optional custom tool descriptions override.
        tool_token_limit_before_evict: Optional token limit before evicting a tool result.

    Example:
        ```python
        from automation.agents.deepagent.filesystem_backend import DAIVFilesystemBackend
        from automation.agents.deepagent.filesystem_middleware import DAIVFilesystemMiddleware

        backend = DAIVFilesystemBackend(root_dir="/workspace", virtual_mode=True)
        middleware = DAIVFilesystemMiddleware(backend=backend)
        ```
    """

    def __init__(self, *args, read_only: bool = False, **kwargs) -> None:
        system_prompt = DAIV_FILESYSTEM_SYSTEM_PROMPT if not read_only else None
        custom_tool_descriptions = kwargs.pop("custom_tool_descriptions", {})

        super().__init__(
            *args, system_prompt=system_prompt, custom_tool_descriptions=custom_tool_descriptions, **kwargs
        )

        if not read_only:
            self.tools.extend([
                _delete_tool_generator(self.backend, custom_tool_descriptions.get("delete")),
                _rename_tool_generator(self.backend, custom_tool_descriptions.get("rename")),
            ])
        else:
            self.tools = [tool for tool in self.tools if tool.name not in ["edit_file", "write_file"]]
