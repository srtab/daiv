from __future__ import annotations

import shutil

from deepagents.backends.filesystem import FilesystemBackend as BaseFilesystemBackend

from .protocol import DeleteResult, RenameResult


class FilesystemBackend(BaseFilesystemBackend):
    """
    Extended FilesystemBackend with delete and rename operations.

    Adds delete (with recursive directory support) and rename (no overwrite)
    capabilities while preserving virtual_mode path safety and traversal protection.
    """

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """
        Delete a file or directory from the filesystem.

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
        """
        Rename a file or directory in the filesystem.

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
