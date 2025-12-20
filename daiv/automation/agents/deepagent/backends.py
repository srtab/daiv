"""Extended FilesystemBackend with delete and rename support for DAIV deep agent."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend as BaseFilesystemBackend
from deepagents.backends.state import StateBackend as BaseStateBackend


@dataclass
class DeleteResult:
    """Result from backend delete operations.

    Attributes:
        error: Error message on failure, None on success.
        path: Absolute path of deleted file/directory, None on failure.
        files_update: State update dict for checkpoint backends, None for external storage.
            Checkpoint backends populate this with {path: None} for deletions.
            External backends set None (already deleted from disk/S3/database/etc).
    """

    error: str | None = None
    path: str | None = None
    files_update: dict[str, None] | None = None


@dataclass
class RenameResult:
    """Result from backend rename operations.

    Attributes:
        error: Error message on failure, None on success.
        old_path: Original path, None on failure.
        new_path: New path, None on failure.
        files_update: State update dict for checkpoint backends, None for external storage.
            Checkpoint backends populate with {old_path: None, new_path: file_data}.
            External backends set None (already persisted).
    """

    error: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    files_update: dict[str, None] | None = None


class FilesystemBackend(BaseFilesystemBackend):
    """Extended FilesystemBackend with delete and rename operations.

    Adds delete (with recursive directory support) and rename (no overwrite)
    capabilities while preserving virtual_mode path safety and traversal protection.
    """

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """Delete a file or directory from the filesystem.

        Args:
            path: Virtual path to delete (absolute when virtual_mode=True).
            recursive: If True, allows deleting directories recursively.
                      If False, attempting to delete a directory will error.

        Returns:
            DeleteResult with error on failure, path on success.
            files_update is always None for FilesystemBackend (external storage).

        Examples:
            >>> backend.delete("/file.txt")  # Delete file
            >>> backend.delete("/dir", recursive=True)  # Delete directory and contents
            >>> backend.delete("/dir")  # Error: directory requires recursive=True
        """
        try:
            resolved_path = self._resolve_path(path)
        except ValueError as e:
            return DeleteResult(error=str(e))

        if not resolved_path.exists():
            return DeleteResult(error=f"Error: Path '{path}' does not exist")

        try:
            if resolved_path.is_file() or resolved_path.is_symlink():
                # Delete file or symlink
                resolved_path.unlink()
                return DeleteResult(path=path, files_update=None)
            elif resolved_path.is_dir():
                if not recursive:
                    return DeleteResult(
                        error=f"Error: Path '{path}' is a directory. Use recursive=True to delete directories."
                    )
                # Delete directory recursively
                shutil.rmtree(resolved_path)
                return DeleteResult(path=path, files_update=None)
            else:
                return DeleteResult(error=f"Error: Path '{path}' is neither a file nor a directory")
        except (OSError, PermissionError) as e:
            return DeleteResult(error=f"Error deleting '{path}': {e}")

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """Async version of delete."""
        import asyncio

        return await asyncio.to_thread(self.delete, path, recursive=recursive)

    def rename(self, old_path: str, new_path: str) -> RenameResult:
        """Rename a file or directory.

        Args:
            old_path: Virtual path of file/directory to rename.
            new_path: Virtual path for the new name.

        Returns:
            RenameResult with error on failure, both paths on success.
            files_update is always None for FilesystemBackend (external storage).

        Behavior:
            - Errors if old_path doesn't exist
            - Errors if new_path already exists (no overwrite)
            - Creates parent directories for new_path if needed
            - Works for both files and directories

        Examples:
            >>> backend.rename("/old.txt", "/new.txt")
            >>> backend.rename("/old_dir", "/new_dir")
        """
        try:
            resolved_old = self._resolve_path(old_path)
            resolved_new = self._resolve_path(new_path)
        except ValueError as e:
            return RenameResult(error=str(e))

        if not resolved_old.exists():
            return RenameResult(error=f"Error: Path '{old_path}' does not exist")

        if resolved_new.exists():
            return RenameResult(error=f"Error: Path '{new_path}' already exists")

        try:
            # Create parent directories for new path if needed
            resolved_new.parent.mkdir(parents=True, exist_ok=True)

            # Rename/move the file or directory
            resolved_old.rename(resolved_new)

            return RenameResult(old_path=old_path, new_path=new_path, files_update=None)
        except (OSError, PermissionError) as e:
            return RenameResult(error=f"Error renaming '{old_path}' to '{new_path}': {e}")

    async def arename(self, old_path: str, new_path: str) -> RenameResult:
        """Async version of rename."""
        import asyncio

        return await asyncio.to_thread(self.rename, old_path, new_path)


class StateBackend(BaseStateBackend):
    """Extended StateBackend with delete and rename operations.

    Adds delete (with recursive directory support) and rename (no overwrite)
    capabilities. Operations return files_update dicts with deletion markers
    (None values) for integration with LangGraph state management.
    """

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """Delete a file or directory from state.

        Args:
            path: Absolute virtual path to delete (must start with '/').
            recursive: If True, allows deleting directories (all paths with prefix).
                      If False, attempting to delete a directory will error.

        Returns:
            DeleteResult with files_update containing deletion markers.
            For files: {path: None}
            For directories: {matching_path1: None, matching_path2: None, ...}

        Examples:
            >>> backend.delete("/file.txt")
            >>> backend.delete("/dir", recursive=True)  # Deletes /dir/file1, /dir/sub/file2, etc.
        """
        files = self.runtime.state.get("files", {})

        if path not in files:
            dir_prefix = path if path.endswith("/") else path + "/"
            matching_files = [k for k in files if k.startswith(dir_prefix)]

            if matching_files:
                if not recursive:
                    return DeleteResult(
                        error=f"Error: Path '{path}' is a directory. Use recursive=True to delete directories."
                    )
                files_update: dict[str, None] = dict.fromkeys(matching_files)
                return DeleteResult(path=path, files_update=files_update)
            else:
                return DeleteResult(error=f"Error: Path '{path}' does not exist")

        return DeleteResult(path=path, files_update={path: None})

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """Async version of delete."""
        import asyncio

        return await asyncio.to_thread(self.delete, path, recursive=recursive)

    def rename(self, old_path: str, new_path: str) -> RenameResult:
        """Rename a file or directory in state.

        Args:
            old_path: Absolute virtual path of file/directory to rename.
            new_path: Absolute virtual path for the new name.

        Returns:
            RenameResult with files_update containing:
            - For files: {old_path: None, new_path: file_data}
            - For directories: {old_path1: None, new_path1: data1, old_path2: None, ...}

        Behavior:
            - Errors if old_path doesn't exist
            - Errors if new_path already exists (no overwrite)
            - For directories, renames all files with that prefix

        Examples:
            >>> backend.rename("/old.txt", "/new.txt")
            >>> backend.rename("/old_dir", "/new_dir")  # Renames all /old_dir/* to /new_dir/*
        """
        files = self.runtime.state.get("files", {})

        new_dir_prefix = new_path if new_path.endswith("/") else new_path + "/"
        if new_path in files or any(k.startswith(new_dir_prefix) for k in files):
            return RenameResult(error=f"Error: Path '{new_path}' already exists")

        if old_path in files:
            file_data = files[old_path]
            files_update: dict[str, Any] = {old_path: None, new_path: file_data}
            return RenameResult(old_path=old_path, new_path=new_path, files_update=files_update)

        old_dir_prefix = old_path if old_path.endswith("/") else old_path + "/"
        matching_files = [(k, v) for k, v in files.items() if k.startswith(old_dir_prefix)]

        if not matching_files:
            return RenameResult(error=f"Error: Path '{old_path}' does not exist")

        files_update = {}
        for old_key, file_data in matching_files:
            relative_path = old_key[len(old_dir_prefix) :]
            new_key = new_dir_prefix + relative_path
            files_update[old_key] = None
            files_update[new_key] = file_data

        return RenameResult(old_path=old_path, new_path=new_path, files_update=files_update)

    async def arename(self, old_path: str, new_path: str) -> RenameResult:
        """Async version of rename."""
        import asyncio

        return await asyncio.to_thread(self.rename, old_path, new_path)
