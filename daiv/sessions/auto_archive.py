"""Archive, for everyone, the sessions of a merge request that merged.

Every session that worked on it is archived (:meth:`SessionQuerySet.on_merge_request`), unless it still has live work:
another merge request it owns is open, or a leg it delegated owns one or is still running. A coordinator waiting on its
legs is archived when the last of them is, if a merge request it owns has merged. A session already archived keeps its
``archived_at``, so a redelivered merge archives nothing new.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

from sessions.live.notify import asession_changed
from sessions.models import MergeRequestOwnership, Session

if TYPE_CHECKING:
    from sessions.managers import SessionQuerySet

logger = logging.getLogger("daiv.sessions")


async def aarchive_merged(repo_id: str, merge_request_iid: int) -> list[str]:
    """Archive the sessions of ``repo_id``'s merged merge request ``merge_request_iid``; returns their thread ids."""
    archived = await _aarchive(Session.objects.on_merge_request(repo_id, merge_request_iid))
    if archived:
        coordinators = Session.objects.filter(
            pk__in=Session.objects.filter(pk__in=archived, parent__isnull=False).values("parent_id")
        ).filter(pk__in=MergeRequestOwnership.objects.filter(merged=True).values("session_id"))
        archived += await _aarchive(coordinators)
    if archived:
        logger.info("Archived the sessions of merged %s!%s: %s", repo_id, merge_request_iid, ", ".join(archived))
    for thread_id in archived:
        await asession_changed(thread_id)
    return archived


async def _aarchive(sessions: SessionQuerySet) -> list[str]:
    """Archive each of ``sessions`` that is unarchived and has no live work; returns the thread ids it archived."""
    now = timezone.now()
    eligible = sessions.filter(archived_at__isnull=True).without_live_work()
    archived = []
    async for thread_id in eligible.values_list("pk", flat=True):
        if await Session.objects.filter(pk=thread_id, archived_at__isnull=True).aupdate(archived_at=now):
            archived.append(thread_id)
    return archived
