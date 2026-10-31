"""The cron sweep that re-reads CI a webhook did not report.

A watch observes until its merge request is merged or closed, so nothing here expires. What the sweep repairs is a
missed pipeline event: an open, watched merge request whose latest DAIV commit has no CI result after
``WATCH_STALE_AFTER`` has its branch's latest pipeline judged, for up to ``WATCH_POLL_HORIZON`` after DAIV pushed.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from sessions.pipeline_watch.service import PipelineWatch
from sessions.pipeline_watch.store import WatchStore

logger = logging.getLogger("daiv.sessions")

WATCH_STALE_AFTER = timedelta(minutes=30)
# A repository whose merge requests get no CI at all would otherwise be polled until each one closed.
WATCH_POLL_HORIZON = timedelta(hours=6)
# Bounded per tick so a backlog never pins a worker on an unbounded fan-out of platform reads.
WATCH_SWEEP_LIMIT = 200


class WatchReconciler:
    """Re-judge the branches whose CI result never arrived. ``watch_factory`` is per-repository, because ``repo_id``
    binds to a ``PipelineWatch`` and one sweep spans repositories."""

    def __init__(self, *, watch_factory=None) -> None:
        self._store = WatchStore()
        self._watch_factory = watch_factory or PipelineWatch

    async def areconcile(self) -> int:
        """Returns the branches re-judged."""
        now = timezone.now()
        touched = 0
        async for ownership in self._store.unjudged(
            quiet_since=now - WATCH_STALE_AFTER, horizon=now - WATCH_POLL_HORIZON, limit=WATCH_SWEEP_LIMIT
        ):
            try:
                await self._watch_factory(ownership.repo_id).aevaluate(ref=ownership.source_branch, pipeline_id=None)
            except Exception:
                logger.exception(
                    "pipeline_watch: reconcile failed for %s@%s", ownership.repo_id, ownership.source_branch
                )
            touched += 1
        return touched
