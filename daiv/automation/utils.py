from __future__ import annotations


def file_changes_namespace(repo_id: str, ref: str) -> tuple[str, ...]:
    """
    Get the namespace for the file changes.

    Args:
        repo_id: The ID of the source repository.
        ref: The reference of the source repository.

    Returns:
        The namespace for the file changes.
    """
    return ("file_changes", repo_id, ref)
