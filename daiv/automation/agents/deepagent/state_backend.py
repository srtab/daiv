"""Extended StateBackend with delete and rename support for DAIV deep agent."""

from __future__ import annotations

from typing import Any

from deepagents.backends.state import StateBackend

from .filesystem_backend import DeleteResult, RenameResult


class DAIVStateBackend(StateBackend):
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
            # Check if it's a directory (any files start with path + "/")
            dir_prefix = path if path.endswith("/") else path + "/"
            matching_files = [k for k in files if k.startswith(dir_prefix)]

            if matching_files:
                # It's a directory
                if not recursive:
                    return DeleteResult(
                        error=f"Error: Path '{path}' is a directory. Use recursive=True to delete directories."
                    )
                # Delete all files in directory
                files_update: dict[str, None] = dict.fromkeys(matching_files)
                return DeleteResult(path=path, files_update=files_update)
            else:
                return DeleteResult(error=f"Error: Path '{path}' does not exist")

        # It's a file - delete it
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

        # Check if new_path already exists
        new_dir_prefix = new_path if new_path.endswith("/") else new_path + "/"
        if new_path in files or any(k.startswith(new_dir_prefix) for k in files):
            return RenameResult(error=f"Error: Path '{new_path}' already exists")

        # Check if old_path is a file
        if old_path in files:
            file_data = files[old_path]
            files_update: dict[str, Any] = {old_path: None, new_path: file_data}
            return RenameResult(old_path=old_path, new_path=new_path, files_update=files_update)

        # Check if old_path is a directory
        old_dir_prefix = old_path if old_path.endswith("/") else old_path + "/"
        matching_files = [(k, v) for k, v in files.items() if k.startswith(old_dir_prefix)]

        if not matching_files:
            return RenameResult(error=f"Error: Path '{old_path}' does not exist")

        # Rename all files in directory
        files_update = {}
        for old_key, file_data in matching_files:
            # Replace old prefix with new prefix
            relative_path = old_key[len(old_dir_prefix) :]
            new_key = new_dir_prefix + relative_path
            files_update[old_key] = None
            files_update[new_key] = file_data

        return RenameResult(old_path=old_path, new_path=new_path, files_update=files_update)

    async def arename(self, old_path: str, new_path: str) -> RenameResult:
        """Async version of rename."""
        import asyncio

        return await asyncio.to_thread(self.rename, old_path, new_path)
