from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, cast

from codebase.base import FileChange
from codebase.context import get_repository_ctx

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore

    from codebase.base import FileChangeAction


def file_changes_namespace(repo_id: str, ref: str) -> tuple[str, ...]:
    """
    Namespace to register file changes in the store.

    Args:
        repo_id: The ID of the source repository.
        ref: The reference of the source repository.

    Returns:
        The store namespace for the file changes.
    """
    return (repo_id, ref, "file_changes")


def file_reads_namespace(repo_id: str, ref: str) -> tuple[str, ...]:
    """
    Namespace to register file reads in the store.

    Args:
        repo_id: The ID of the source repository.
        ref: The reference of the source repository.

    Returns:
        The store namespace for the file reads.
    """
    return (repo_id, ref, "file_reads")


async def register_file_change(
    store: BaseStore,
    action: FileChangeAction,
    old_file_content: str,
    old_file_path: str,
    new_file_content: str | None = None,
    new_file_path: str | None = None,
):
    """
    Register file change in the store.

    Args:
        store: The store to use for caching.
        action: The action to register.
        old_file_content: The content of the old file.
        old_file_path: The path to the old file.
        new_file_content: The content of the new file. If not provided, the old file content will be used.
        new_file_path: The path to the new file. If not provided, the old file path will be used.
    """
    ctx = get_repository_ctx()

    new_file_content = new_file_content or old_file_content
    new_file_path = new_file_path or old_file_path

    diff_from_file = f"a/{old_file_path}"
    diff_to_file = f"b/{new_file_path}"

    if action.CREATE:
        diff_from_file = "a/dev/null"
    elif action.DELETE:
        diff_to_file = "a/dev/null"

    diff_hunk = difflib.unified_diff(
        old_file_content.splitlines(),
        new_file_content.splitlines(),
        fromfile=diff_from_file,
        tofile=diff_to_file,
        lineterm="",
    )

    await store.aput(
        namespace=file_changes_namespace(ctx.repo_id, ctx.ref),
        key=new_file_path,
        value={
            "data": FileChange(
                action=action, file_path=new_file_path, content=new_file_content, diff_hunk="\n".join(diff_hunk)
            )
        },
    )


async def has_file_changes(store: BaseStore) -> bool:
    """
    Check if there are any file changes.

    Args:
        store: The store to use for caching.

    Returns:
        True if there are any file changes, False otherwise.
    """
    ctx = get_repository_ctx()
    namespace = file_changes_namespace(ctx.repo_id, ctx.ref)

    return bool(await store.asearch(namespace, limit=1))


async def get_file_changes(store: BaseStore) -> list[FileChange]:
    """
    Get all file changes from the store.

    Args:
        store: The store to use for caching.

    Returns:
        A list of file changes.
    """
    ctx = get_repository_ctx()
    namespace = file_changes_namespace(ctx.repo_id, ctx.ref)

    return [cast("FileChange", change.value["data"]) for change in await store.asearch(namespace)]


async def get_file_change(store: BaseStore, file_path: str) -> FileChange | None:
    """
    Get a file change from the store.

    Args:
        store: The store to use for caching.
        file_path: The path to the file to get the change for.

    Returns:
        The file change for the given file path, or None if no change has been registered.
    """
    ctx = get_repository_ctx()

    namespace = file_changes_namespace(ctx.repo_id, ctx.ref)

    if stored_file_change := await store.aget(namespace=namespace, key=file_path):
        return cast("FileChange", stored_file_change.value["data"])

    return None


async def register_file_read(store: BaseStore, file_path: str):
    """
    Register file read in the store.

    Args:
        store: The store to use for caching.
        file_path: The path to the file that was read.
    """
    ctx = get_repository_ctx()
    namespace = file_reads_namespace(ctx.repo_id, ctx.ref)

    await store.aput(namespace=namespace, key=file_path, value={"data": True})


async def check_file_read(store: BaseStore, file_path: str) -> bool:
    """
    Check if a file has been read.

    Args:
        store: The store to use for caching.
        file_path: The path to the file to check.

    Returns:
        True if the file has been read, False otherwise.
    """
    ctx = get_repository_ctx()
    namespace = file_reads_namespace(ctx.repo_id, ctx.ref)

    return await store.aget(namespace=namespace, key=file_path) is not None
