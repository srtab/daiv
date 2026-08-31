"""The cron sweep that repairs watches events did not resolve."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext as _

from sessions.locks import stale_cutoff
from sessions.models import WatchState
from sessions.pipeline_watch.platform import WatchPlatform
from sessions.pipeline_watch.service import PipelineWatch
from sessions.pipeline_watch.store import WatchStore

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger("daiv.sessions")

WATCH_MAX_AGE = timedelta(hours=6)
# How long to wait for a pipeline event before polling the branch ourselves. Unrelated to
# ``STALE_RUN_MINUTES``, which is what says a *fix run* died.
WATCH_STALE_AFTER = timedelta(minutes=30)
# Bounded per tick so a backlog never pins a worker on an unbounded fan-out of platform reads.
WATCH_SWEEP_LIMIT = 200


class WatchReconciler:
    """Repair watches that events did not resolve.

    The collaborators are per-repository factories rather than instances because ``repo_id`` binds
    to a ``WatchPlatform`` and one sweep spans repositories. That costs nothing per row:
    ``RepoClient.create_instance`` is process-cached, so a fresh platform's first read is a thread
    hop over a dict lookup, not another installation lookup.
    """

    def __init__(self, *, watch_factory=None, platform_factory=None) -> None:
        self._store = WatchStore()
        self._watch_factory = watch_factory or PipelineWatch
        self._platform_factory = platform_factory or WatchPlatform

    async def areconcile(self) -> int:
        """Three jobs, and the order is load-bearing: expiring first is what bounds the two sweeps
        below to a live watch, so neither needs an age floor of its own. Returns watches expired,
        plus rows the bulk un-stick updated, plus branches re-judged — a progress signal, not a
        count of rows written.
        """
        now = timezone.now()
        touched = await self._aexpire(now)
        # A fix run that stopped heartbeating is dead by the same definition SessionLock uses to
        # take its slot over; the two have to move together.
        touched += await self._store.arecover_stale_fixing(cutoff=stale_cutoff(now), now=now)
        touched += await self._arejudge(now)
        return touched

    async def _aexpire(self, now: datetime) -> int:
        touched = 0
        async for session in self._store.expiring_watches(cutoff=now - WATCH_MAX_AGE, limit=WATCH_SWEEP_LIMIT):
            if not await self._store.atransition(
                session.thread_id, expect=WatchState.open(), watch_state=WatchState.UNCLEAR
            ):
                continue
            # Where every unresolved failure lands — unreadable pipeline, dead fix run, pipeline
            # that never started. Closing it silently made those indistinguishable.
            logger.warning(
                "pipeline_watch: giving up on %s!%s after %s in state %s",
                session.repo_id,
                session.merge_request_iid,
                WATCH_MAX_AGE,
                session.watch_state,
            )
            await self._platform_factory(session.repo_id).apost_note(
                merge_request_iid=session.merge_request_iid,
                body=_(
                    "I stopped watching CI on this merge request: no result I could act on arrived "
                    "within {hours} hours. Mention me if you want another look."
                ).format(hours=int(WATCH_MAX_AGE.total_seconds() // 3600)),
            )
            touched += 1
        return touched

    async def _arejudge(self, now: datetime) -> int:
        touched = 0
        async for repo_id, ref, thread_id in self._store.stale_watching(
            cutoff=now - WATCH_STALE_AFTER, limit=WATCH_SWEEP_LIMIT
        ):
            try:
                await self._watch_factory(repo_id).aevaluate(ref=ref, pipeline_id=None)
            except Exception:
                logger.exception("pipeline_watch: reconcile failed for thread_id=%s", thread_id)
            touched += 1
        return touched
