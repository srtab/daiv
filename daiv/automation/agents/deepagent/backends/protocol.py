from __future__ import annotations

from dataclasses import dataclass


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
