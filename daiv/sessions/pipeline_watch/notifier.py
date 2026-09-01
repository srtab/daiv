"""The give-up notification. Best-effort — the MR comment is the reliable channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async

if TYPE_CHECKING:
    from sessions.models import Session
    from sessions.pipeline_watch.judgment import PipelineReport

logger = logging.getLogger("daiv.sessions")


class WatchNotifier:
    async def anotify_exhausted(self, *, session: Session, report: PipelineReport) -> None:
        """Report that the watch gave up.

        Reading the pipeline is this package's job; the source key, channels, dedup and payload are
        the notifications app's.
        """
        from notifications.watch_notifiers import emit_watch_exhausted

        try:
            await sync_to_async(emit_watch_exhausted)(
                session=session,
                failing_jobs=[job.name for job in report.failed_jobs],
                pipeline_url=report.web_url,
                pipeline_id=report.id,
            )
        except Exception:
            logger.exception("pipeline_watch: failed to notify the owner of thread_id=%s", session.thread_id)
