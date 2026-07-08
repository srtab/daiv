from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from sessions.locks import STALE_RUN_MINUTES
from sessions.models import Run, RunStatus, SessionOrigin

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = (
        "Reconcile non-terminal Run rows. Task-backed runs are re-synced from their linked "
        "DBTaskResult; inline chat runs (which have no task result) that a worker crash left "
        "stuck in RUNNING are failed once their session heartbeat goes stale."
    )

    def handle(self, *args, **options):
        qs = (
            Run.objects
            .filter(task_result__isnull=False)
            .exclude(status__in=list(RunStatus.terminal()))
            .select_related("task_result", "session", "session__scheduled_job")
        )

        synced = skipped = errored = 0
        for run in qs.iterator():
            try:
                if run.sync_and_save():
                    synced += 1
                else:
                    skipped += 1
            except Exception:
                errored += 1
                logger.exception("Failed to sync run %s", run.id)

        reaped = self._reap_orphaned_chat_runs()

        summary = f"Synced: {synced}, already up to date: {skipped}, errored: {errored}, chat runs reaped: {reaped}"
        if errored:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))

    def _reap_orphaned_chat_runs(self) -> int:
        """Fail chat runs orphaned by a hard worker crash.

        Chat turns run inline with no DBTaskResult, so the DBTaskResult sync above can never
        reconcile one whose streamer ``finally`` never ran (OOM / SIGKILL before it could
        finalize). We reap those using the same staleness signal ``SessionLock`` uses to
        decide a holder is dead — the session heartbeat (``last_active_at``), bumped every few
        seconds by a live stream — so a genuinely long-running chat turn is never reaped.

        A direct ``.update()`` (no ``run_finished`` emit) mirrors ``finalize_chat_run``: chat
        runs are intentionally excluded from the notification / memory / dispatch receivers.
        """
        cutoff = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES)
        reaped = Run.objects.filter(
            trigger_type=SessionOrigin.CHAT,
            task_result__isnull=True,
            status=RunStatus.RUNNING,
            session__last_active_at__lt=cutoff,
        ).update(
            status=RunStatus.FAILED,
            finished_at=timezone.now(),
            error_message="Orphaned chat run: streamer never finalized (worker crash); reaped by sync_stuck_runs.",
        )
        if reaped:
            logger.warning("sync_stuck_runs: reaped %d orphaned chat run(s) stuck in RUNNING", reaped)
        return reaped
