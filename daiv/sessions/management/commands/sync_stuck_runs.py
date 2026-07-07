from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from sessions.models import Run, RunStatus

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = "Re-sync non-terminal Run rows from their linked DBTaskResult."

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

        summary = f"Synced: {synced}, already up to date: {skipped}, errored: {errored}"
        if errored:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
