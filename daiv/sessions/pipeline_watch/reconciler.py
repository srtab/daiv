"""The cron sweep that re-reads CI a webhook did not report.

A watch observes until its merge request is merged or closed, so nothing here expires. What the sweep repairs is any
missed pipeline event, a manual retry's pass included: an open, watched merge request nothing was heard of for
``WATCH_STALE_AFTER`` (no CI result, no claim, and no re-read by this sweep) has the latest pipeline of each of its
branch's CI subjects judged again: GitLab's one pipeline, or the newest run of each GitHub workflow, so a missed
workflow's result is repaired even when another workflow ran after it. A pipeline already judged in its status is
skipped before its jobs are read (``PipelineWatch.aevaluate_latest``), so the cost is mostly one listing per branch.

At most ``WATCH_SWEEP_LIMIT`` branches are read per tick, stalest first. A read counts as hearing of the branch
(``MergeRequestOwnership.last_reconciled_at``), so a backlog rotates through every branch. A branch that could not be
read in full counts as heard of ``WATCH_RETRY_AFTER`` short of the window, so it is read again on the next tick
without starving the others.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from sessions.pipeline_watch.service import PipelineWatch
from sessions.pipeline_watch.store import WatchStore

logger = logging.getLogger("daiv.sessions")

WATCH_STALE_AFTER = timedelta(minutes=30)
WATCH_RETRY_AFTER = timedelta(minutes=5)
# Bounded per tick so a backlog never pins a worker on an unbounded fan-out of platform reads.
WATCH_SWEEP_LIMIT = 200


class WatchReconciler:
    """Re-judge the branches nothing was heard of. ``watch_factory`` is per-repository, because ``repo_id`` binds to a
    ``PipelineWatch`` and one sweep spans repositories."""

    def __init__(self, *, watch_factory=None) -> None:
        self._store = WatchStore()
        self._watch_factory = watch_factory or PipelineWatch

    async def areconcile(self) -> int:
        """Returns the branches re-read."""
        now = timezone.now()
        due = [row async for row in self._store.quiet(since=now - WATCH_STALE_AFTER, limit=WATCH_SWEEP_LIMIT)]
        for row in due:
            try:
                read = await self._watch_factory(row["repo_id"]).aevaluate_latest(ref=row["source_branch"])
            except Exception:
                logger.exception("pipeline_watch: reconcile failed for %s@%s", row["repo_id"], row["source_branch"])
                read = False
            at = now if read else now - WATCH_STALE_AFTER + WATCH_RETRY_AFTER
            await self._store.amark_reconciled(row["pk"], at=at)
        return len(due)
