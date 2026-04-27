"""Helpers that pull repo/branch/MR state out of the chat agent's graph state.

The chat UI's MR pill is driven by ``merge_request`` written back into LangGraph
state by ``GitMiddleware``. The live path streams it via AG-UI ``STATE_SNAPSHOT``
events; this module provides the server-rendered fallback used to seed the page
on first load (and to surface a pre-existing MR before the agent has run).
"""

from __future__ import annotations

import logging
from typing import Any

from asgiref.sync import sync_to_async

from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig

logger = logging.getLogger("daiv.chat")


def mr_to_payload(mr: Any) -> dict[str, Any] | None:
    """Normalize a stored MergeRequest into a JSON payload for the UI.

    Accepts either a ``codebase.base.MergeRequest`` instance (live state) or a
    plain dict (re-hydrated from the checkpointer). Returns ``None`` if there's
    no MR to surface.
    """
    if mr is None:
        return None
    if hasattr(mr, "model_dump"):
        data = mr.model_dump()
    elif isinstance(mr, dict):
        data = mr
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

    Used to surface a pre-existing MR in the composer when LangGraph state has
    none — e.g. the chat opened on a branch whose MR was created in a prior
    conversation, by the agent on a different thread, or by a human via the
    platform UI. Returns ``None`` for the default branch, missing inputs, or
    any platform error (logged).
    """
    if not repo_id or not ref:
        return None
    try:
        config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
        if ref == config.default_branch:
            return None
        client = RepoClient.create_instance()
        mr = await sync_to_async(client.get_merge_request_by_branches)(repo_id, ref, config.default_branch)
    except Exception:
        logger.exception("Failed to look up existing merge request for %s on %s", repo_id, ref)
        return None
    return mr_to_payload(mr)
