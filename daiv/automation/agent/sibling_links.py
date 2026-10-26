from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

if TYPE_CHECKING:
    from collections.abc import Sequence

    from codebase.base import MergeRequest
    from codebase.clients.base import RepoClient

logger = logging.getLogger("daiv.tools")

RELATED_START = "<!-- daiv:related-merge-requests -->"
RELATED_END = "<!-- /daiv:related-merge-requests -->"

_RELATED_BLOCK = re.compile(f"{re.escape(RELATED_START)}.*?{re.escape(RELATED_END)}", re.DOTALL)


@dataclass(frozen=True)
class RelatedMergeRequest:
    client: RepoClient
    repo_slug: str
    merge_request: MergeRequest


def render_related_block(related: Sequence[RelatedMergeRequest]) -> str:
    lines = [
        f"- `{entry.repo_slug}`: [{entry.merge_request.title}]({entry.merge_request.web_url})" for entry in related
    ]
    return "\n".join([RELATED_START, "**Related merge requests** (same DAIV session):", "", *lines, RELATED_END])


def with_related_block(description: str, block: str) -> str:
    """``description`` with its related-merge-requests block replaced by ``block``, or ``block`` appended."""
    if _RELATED_BLOCK.search(description):
        return _RELATED_BLOCK.sub(lambda _: block, description, count=1)
    if not description.strip():
        return block
    return f"{description.rstrip()}\n\n{block}"


async def aensure_related_links(entries: Sequence[RelatedMergeRequest]) -> None:
    """Make each merge request's description list the others. Best effort: a failed update is logged, never raised."""
    if len(entries) < 2:
        return
    for entry in entries:
        merge_request = entry.merge_request
        try:
            others = [other for other in entries if other is not entry]
            description = with_related_block(merge_request.description or "", render_related_block(others))
            if description == (merge_request.description or ""):
                continue
            await sync_to_async(entry.client.update_merge_request)(
                merge_request.repo_id, merge_request.merge_request_id, description=description
            )
        except Exception:
            logger.exception(
                "Could not link %s !%s to its session's other merge requests",
                entry.repo_slug,
                merge_request.merge_request_id,
            )
