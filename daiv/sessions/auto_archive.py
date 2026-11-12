"""Archive, for everyone, the sessions of a merge request that merged.

Every session that worked on it is archived (:meth:`SessionQuerySet.on_merge_request`), unless it still has live work
(:meth:`SessionQuerySet.without_live_work`). One that owns another open merge request is archived when that one merges.
One held back only by an unfinished run, its own or a leg's, or by a leg owning an open merge request is marked
``archive_pending``, and archived once that work settles: when a run of its own or of one of its legs ends
(:func:`archive_settled`), or when a leg of it is archived on a merge. Each merge is handled once (``ArchivedMerge``),
so a redelivered merge archives nothing, not even a session someone has unarchived since.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, models, transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.live.notify import asession_changed, session_changed
from sessions.models import ArchivedMerge, Session

if TYPE_CHECKING:
    from sessions.managers import SessionQuerySet

logger = logging.getLogger("daiv.sessions")


async def aarchive_merged(repo_id: str, merge_request_iid: int) -> list[str]:
    """Archive the sessions of ``repo_id``'s merged merge request ``merge_request_iid``; returns their thread ids."""
    archived = await sync_to_async(_archive_merged)(repo_id, merge_request_iid)
    if archived:
        logger.info("Archived the sessions of merged %s!%s: %s", repo_id, merge_request_iid, ", ".join(archived))
    await asyncio.gather(*(asession_changed(thread_id) for thread_id in archived))
    return archived


@transaction.atomic
def _archive_merged(repo_id: str, merge_request_iid: int) -> list[str]:
    try:
        with transaction.atomic():
            ArchivedMerge.objects.create(repo_id=repo_id, merge_request_iid=merge_request_iid)
    except IntegrityError:
        return []
    on_it = Session.objects.on_merge_request(merge_request_iid, repo_id=repo_id)
    on_it.filter(archived_at__isnull=True).without_open_merge_request().update(archive_pending=True)
    archived = _archive(on_it)
    if archived:
        coordinators = Session.objects.filter(pk__in=archived, parent__isnull=False).values("parent_id")
        archived += _archive(Session.objects.filter(pk__in=coordinators, archive_pending=True))
    return archived


def archive_settled(thread_id: str) -> list[str]:
    """Archive ``thread_id`` and the coordinator that delegated it, each if a merge left it pending and none of its work
    is live any more; returns the thread ids it archived."""
    parent = Session.objects.filter(pk=thread_id).values("parent_id")
    archived = _archive(Session.objects.filter(models.Q(pk=thread_id) | models.Q(pk__in=parent), archive_pending=True))
    for archived_id in archived:
        session_changed(archived_id)
    return archived


def _archive(sessions: SessionQuerySet) -> list[str]:
    """Archive each of ``sessions`` that is unarchived and has no live work; returns the thread ids it archived.

    The live-work check is part of the ``UPDATE``, so work that starts after ``sessions`` was read keeps its session.
    """
    candidates = list(sessions.filter(archived_at__isnull=True).values_list("pk", flat=True))
    if not candidates:
        return []
    now = timezone.now()
    Session.objects.filter(pk__in=candidates, archived_at__isnull=True).without_live_work().update(
        archived_at=now, archive_pending=False
    )
    return list(Session.objects.filter(pk__in=candidates, archived_at=now).values_list("pk", flat=True))
