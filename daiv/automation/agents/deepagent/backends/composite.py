from __future__ import annotations

from contextlib import suppress

from deepagents.backends.composite import CompositeBackend as BaseCompositeBackend

from .protocol import DeleteResult, RenameResult


class CompositeBackend(BaseCompositeBackend):
    """
    Extended CompositeBackend with delete and rename operations.

    Routes delete and rename operations to the appropriate backend based on path prefix,
    following the same routing logic as other operations like read, write, and edit.
    """

    def delete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """
        Delete a file or directory, routing to appropriate backend.

        Args:
            path: Absolute path to delete.
            recursive: If True, allows deleting directories recursively.
                      If False, attempting to delete a directory will error.

        Returns:
            DeleteResult with error on failure, path on success.
            files_update is merged with default backend state if applicable.

        Examples:
            >>> backend.delete("/file.txt")
            >>> backend.delete("/skills/memory.txt")  # Routes to StateBackend
            >>> backend.delete("/dir", recursive=True)
        """
        backend, stripped_key = self._get_backend_and_key(path)

        if not hasattr(backend, "delete"):
            return DeleteResult(error=f"Error: Deleting {path} is not supported")

        res = backend.delete(stripped_key, recursive=recursive)

        # Restore the original path in the result
        if res.path is not None:
            res.path = path

        # If this is a state-backed update and default has state, merge so listings reflect changes
        if res.files_update:
            with suppress(Exception):
                runtime = getattr(self.default, "runtime", None)
                if runtime is not None:
                    state = runtime.state
                    files = state.get("files", {})
                    files.update(res.files_update)
                    state["files"] = files

        return res

    async def adelete(self, path: str, *, recursive: bool = False) -> DeleteResult:
        """Async version of delete."""
        backend, stripped_key = self._get_backend_and_key(path)

        if not hasattr(backend, "adelete"):
            return DeleteResult(error=f"Error: Deleting {path} is not supported")

        res = await backend.adelete(stripped_key, recursive=recursive)

        # Restore the original path in the result
        if res.path is not None:
            res.path = path

        # If this is a state-backed update and default has state, merge so listings reflect changes
        if res.files_update:
            with suppress(Exception):
                runtime = getattr(self.default, "runtime", None)
                if runtime is not None:
                    state = runtime.state
                    files = state.get("files", {})
                    files.update(res.files_update)
                    state["files"] = files

        return res

    def rename(self, old_path: str, new_path: str) -> RenameResult:
        """
        Rename a file or directory, routing to appropriate backend.

        Args:
            old_path: Absolute path of file/directory to rename.
            new_path: Absolute path for the new name.

        Returns:
            RenameResult with error on failure, both paths on success.
            files_update is merged with default backend state if applicable.

        Behavior:
            - Errors if old_path doesn't exist
            - Errors if new_path already exists (no overwrite)
            - Both paths must route to the same backend (no cross-backend moves)

        Examples:
            >>> backend.rename("/old.txt", "/new.txt")
            >>> backend.rename("/skills/old.txt", "/skills/new.txt")  # Routes to StateBackend
        """
        old_backend, stripped_old_path = self._get_backend_and_key(old_path)
        new_backend, stripped_new_path = self._get_backend_and_key(new_path)

        if old_backend is not new_backend or not hasattr(old_backend, "rename"):
            return RenameResult(error=f"Error: Renaming {old_path} to {new_path} is not supported.")

        res = old_backend.rename(stripped_old_path, stripped_new_path)

        if res.old_path is not None:
            res.old_path = old_path
        if res.new_path is not None:
            res.new_path = new_path

        if res.files_update:
            with suppress(Exception):
                runtime = getattr(self.default, "runtime", None)
                if runtime is not None:
                    state = runtime.state
                    files = state.get("files", {})
                    files.update(res.files_update)
                    state["files"] = files

        return res

    async def arename(self, old_path: str, new_path: str) -> RenameResult:
        """Async version of rename."""
        old_backend, stripped_old_path = self._get_backend_and_key(old_path)
        new_backend, stripped_new_path = self._get_backend_and_key(new_path)

        if old_backend is not new_backend or not hasattr(old_backend, "arename"):
            return RenameResult(error=f"Error: Renaming {old_path} to {new_path} is not supported.")

        res = await old_backend.arename(stripped_old_path, stripped_new_path)

        if res.old_path is not None:
            res.old_path = old_path
        if res.new_path is not None:
            res.new_path = new_path

        if res.files_update:
            with suppress(Exception):
                runtime = getattr(self.default, "runtime", None)
                if runtime is not None:
                    state = runtime.state
                    files = state.get("files", {})
                    files.update(res.files_update)
                    state["files"] = files

        return res
