from __future__ import annotations

from typing import TYPE_CHECKING, cast

from codebase.base import FileChange, FileChangeAction
from codebase.context import get_runtime_ctx

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore


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


def sandbox_session_namespace(repo_id: str, ref: str) -> tuple[str, ...]:
    """
    Namespace to register sandbox session in the store.

    Args:
        repo_id: The ID of the source repository.
        ref: The reference of the source repository.

    Returns:
        The store namespace for the sandbox session.
    """
    return (repo_id, ref, "sandbox_session")


async def register_file_change(
    store: BaseStore,
    action: FileChangeAction,
    old_file_content: str,
    old_file_path: str,
    new_file_content: str | None = None,
    new_file_path: str | None = None,
):
    """
    Register file change in the store to track changes in the repository to be committed later.

    Args:
        store: The store to use for caching.
        action: The action to register.
        old_file_content: The content of the old file.
        old_file_path: The path to the old file.
        new_file_content: The content of the new file. If not provided, the old file content will be used.
        new_file_path: The path to the new file. If not provided, the old file path will be used.
    """
    ctx = get_runtime_ctx()

    new_file_content = new_file_content if new_file_content is not None else old_file_content
    new_file_path = new_file_path or old_file_path

    original_content = None

    if previous_file_change := await get_file_change(store, old_file_path or new_file_path):
        original_content = previous_file_change.original_content

        if previous_file_change.action == FileChangeAction.DELETE and action == FileChangeAction.CREATE:
            # If the file was already registered as a delete action, it means that the file already existed.
            # We need to update the file instead of creating it.
            action = FileChangeAction.UPDATE
            old_file_content = previous_file_change.content
            old_file_path = previous_file_change.file_path
        elif previous_file_change.action == FileChangeAction.CREATE and action == FileChangeAction.DELETE:
            # If the file was already registered as a create action, it means that the file never existed.
            # We need to delete the file change from the store to avoid trying to delete a file that's not on the repo.
            await delete_file_change(store, old_file_path)
            return
        elif previous_file_change.action == FileChangeAction.CREATE and action == FileChangeAction.MOVE:
            # If the file was already registered as a create action, it means that the file never existed.
            # We need to maintain the create action and only update the content, as we can't move a file that
            # never existed.
            action = FileChangeAction.CREATE
            old_file_content = ""
            old_file_path = None
        elif action == FileChangeAction.UPDATE:
            # If the file was already registered with an action, we use the action from the registered file change.
            # For instance, if the registered was a create action, we maintain the create action and only
            # update the content.
            # We don't need to deal with delete action because the file does not exist, so no update will be performed.
            action = previous_file_change.action
            old_file_content = previous_file_change.content
            old_file_path = previous_file_change.file_path

    await store.aput(
        namespace=file_changes_namespace(ctx.repo_id, ctx.ref),
        key=new_file_path,
        value={
            "data": FileChange(
                action=action,
                file_path=new_file_path,
                previous_path=old_file_path,
                original_content=original_content if original_content is not None else old_file_content,
                content=new_file_content,
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
    ctx = get_runtime_ctx()
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
    ctx = get_runtime_ctx()
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
    ctx = get_runtime_ctx()

    namespace = file_changes_namespace(ctx.repo_id, ctx.ref)

    if stored_file_change := await store.aget(namespace=namespace, key=file_path):
        return cast("FileChange", stored_file_change.value["data"])

    return None


async def delete_file_change(store: BaseStore, file_path: str) -> bool:
    """
    Delete a file change from the store.
    """
    ctx = get_runtime_ctx()
    namespace = file_changes_namespace(ctx.repo_id, ctx.ref)
    await store.adelete(namespace=namespace, key=file_path)


async def register_file_read(store: BaseStore, file_path: str):
    """
    Register file read in the store.

    Args:
        store: The store to use for caching.
        file_path: The path to the file that was read.
    """
    ctx = get_runtime_ctx()
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
    ctx = get_runtime_ctx()
    namespace = file_reads_namespace(ctx.repo_id, ctx.ref)

    return await store.aget(namespace=namespace, key=file_path) is not None


async def register_sandbox_session(store: BaseStore, session_id: str):
    """
    Register sandbox session in the store.

    Args:
        store: The store to use for caching.
        session_id: The sandbox session ID.
    """
    ctx = get_runtime_ctx()
    namespace = sandbox_session_namespace(ctx.repo_id, ctx.ref)

    await store.aput(namespace=namespace, key="session_id", value={"data": session_id})


async def get_sandbox_session(store: BaseStore) -> str | None:
    """
    Get the sandbox session ID from the store.

    Args:
        store: The store to use for caching.

    Returns:
        The session ID if found, None otherwise.
    """
    ctx = get_runtime_ctx()
    namespace = sandbox_session_namespace(ctx.repo_id, ctx.ref)

    if stored_session := await store.aget(namespace=namespace, key="session_id"):
        return cast("str", stored_session.value["data"])

    return None


async def delete_sandbox_session(store: BaseStore):
    """
    Delete the sandbox session from the store.

    Args:
        store: The store to use for caching.
    """
    ctx = get_runtime_ctx()
    namespace = sandbox_session_namespace(ctx.repo_id, ctx.ref)

    await store.adelete(namespace=namespace, key="session_id")
