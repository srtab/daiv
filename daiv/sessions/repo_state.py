"""Server-rendered fallbacks for chat MR state.

The live path streams ``merge_request`` via AG-UI ``STATE_SNAPSHOT`` events; this
module seeds the page on first load and surfaces a pre-existing MR before the
agent has run.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from codebase.base import MergeRequest
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig


def mr_to_payload(mr: object) -> dict[str, Any] | None:
    """Normalize a stored MergeRequest into a JSON payload for the UI.

    Accepts a live ``MergeRequest`` instance or a plain dict (re-hydrated from
    the checkpointer). Returns ``None`` for any other input.
    """
    if mr is None:
        return None
    if isinstance(mr, MergeRequest):
        data: dict[str, Any] = mr.model_dump()
    elif isinstance(mr, dict):
        data = dict(mr)
    else:
        return None
    return {
        "id": data.get("merge_request_id"),
        "url": data.get("web_url"),
        "title": data.get("title"),
        "draft": bool(data.get("draft", False)),
        "source_branch": data.get("source_branch"),
        "target_branch": data.get("target_branch"),
    }


async def aget_existing_mr_payload(repo_id: str, ref: str) -> dict[str, Any] | None:
    """Look up an open MR for ``ref`` on the configured git platform.

    Returns ``None`` for the default branch, missing inputs, or no open MR. A platform/transport error
    (:data:`~codebase.clients.base.PLATFORM_ERRORS`) propagates, so the caller can tell an unknown merge request
    from none.
    """
    if not repo_id or not ref:
        return None
    config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
    if ref == config.default_branch:
        return None
    client = RepoClient.create_instance()
    return mr_to_payload(await sync_to_async(client.get_merge_request_by_branches)(repo_id, ref))
