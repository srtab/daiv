from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from codebase.context import get_runtime_ctx

if TYPE_CHECKING:
    from langgraph.store.base import BaseStore


logger = logging.getLogger("daiv.utils")


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


async def register_file_read(store: BaseStore, file_path: str):
    """
    Register file read in the store.

    Args:
        store: The store to use for caching.
        file_path: The path to the file that was read.
    """
    ctx = get_runtime_ctx()
    namespace = file_reads_namespace(ctx.repo_id, ctx.repo.active_branch.name)

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
    namespace = file_reads_namespace(ctx.repo_id, ctx.repo.active_branch.name)

    return await store.aget(namespace=namespace, key=file_path) is not None
