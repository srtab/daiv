"""Server-rendered fallbacks for chat MR state.

The live path streams ``merge_request`` via AG-UI ``STATE_SNAPSHOT`` events; this
module seeds the page on first load and surfaces a pre-existing MR before the
agent has run.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from asgiref.sync import sync_to_async
from github import GithubException
from gitlab.exceptions import GitlabError

from codebase.base import MergeRequest
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig

logger = logging.getLogger("daiv.chat")

# Platform / transport errors that warrant a soft "no MR" fallback. Anything
# else (bugs, ConfigErrors, AttributeError, etc.) propagates so the caller's
# error handling can surface it instead of silently masking it as "no MR".
_PLATFORM_ERRORS: tuple[type[BaseException], ...] = (GitlabError, GithubException, httpx.HTTPError)


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

    Returns ``None`` for the default branch, missing inputs, or known
    platform/transport errors (logged). Other exceptions propagate.
    """
    if not repo_id or not ref:
        return None
    try:
        config = await sync_to_async(RepositoryConfig.get_config)(repo_id)
        if ref == config.default_branch:
            return None
        client = RepoClient.create_instance()
        mr = await sync_to_async(client.get_merge_request_by_branches)(repo_id, ref, config.default_branch)
    except _PLATFORM_ERRORS:
        logger.exception("Failed to look up existing merge request for %s on %s", repo_id, ref)
        return None
    return mr_to_payload(mr)
