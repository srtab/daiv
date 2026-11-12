"""Archive, for everyone, the sessions of a merge request that merged.

Every session that worked on it is archived (:meth:`SessionQuerySet.on_merge_request`), unless it still has live work
(:meth:`SessionQuerySet.without_live_work`). One that owns another open merge request is archived when that one merges.
One held back only by its own unfinished run or waking inbox event, or by a leg's unfinished run or open merge request,
is marked ``archive_pending``, and archived by :func:`archive_settled` once that work ends: when a run of its own or of
one of its legs ends, or when a leg's merge request merges or closes. Each merge is handled once (``ArchivedMerge``),
so a redelivered merge archives nothing, not even a session someone has unarchived since.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.live.notify import session_changed
from sessions.models import ArchivedMerge, Session

logger = logging.getLogger("daiv.sessions")


async def aarchive_merged(repo_id: str, merge_request_iid: int) -> list[str]:
    """Archive the sessions of ``repo_id``'s merged merge request ``merge_request_iid``; returns their thread ids."""
    archived = await sync_to_async(_archive_merged)(repo_id, merge_request_iid)
    if archived:
        logger.info("Archived the sessions of merged %s!%s: %s", repo_id, merge_request_iid, ", ".join(archived))
    return archived


@transaction.atomic
def _archive_merged(repo_id: str, merge_request_iid: int) -> list[str]:
    on_it = Session.objects.on_merge_request(merge_request_iid, repo_id=repo_id).filter(archived_at__isnull=True)
    candidates = list(on_it.values_list("pk", flat=True))
    if not candidates:
        return []
    _, created = ArchivedMerge.objects.get_or_create(repo_id=repo_id, merge_request_iid=merge_request_iid)
    if not created:
        return []
    archived = _archive(candidates)
    Session.objects.filter(pk__in=candidates, archived_at__isnull=True).without_open_merge_request().update(
        archive_pending=True
    )
    return archived


def archive_settled(thread_id: str) -> list[str]:
    """Archive ``thread_id`` and the coordinator that delegated it, each if a merge left it pending and none of its work
    is live any more; returns the thread ids it archived."""
    row = Session.objects.filter(pk=thread_id).values_list("archive_pending", "parent_id", "parent__archive_pending")
    if (found := row.first()) is None:
        return []
    pending, parent_id, parent_pending = found
    return _archive([pk for pk, due in ((thread_id, pending), (parent_id, parent_pending)) if due])


def _archive(thread_ids: list[str]) -> list[str]:
    """Archive each of ``thread_ids`` that is unarchived and has no live work, and poke its page; returns the thread ids
    it archived.

    The live-work check is part of the ``UPDATE``, so work that starts after the caller read ``thread_ids`` keeps its
    session.
    """
    if not thread_ids:
        return []
    now = timezone.now()
    updated = (
        Session.objects
        .filter(pk__in=thread_ids, archived_at__isnull=True)
        .without_live_work()
        .update(archived_at=now, archive_pending=False)
    )
    if updated == len(thread_ids):
        archived = list(thread_ids)
    else:
        archived = list(Session.objects.filter(pk__in=thread_ids, archived_at=now).values_list("pk", flat=True))
    for thread_id in archived:
        session_changed(thread_id)
    return archived
