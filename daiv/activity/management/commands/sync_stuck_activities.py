from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from activity.models import Activity, ActivityStatus

logger = logging.getLogger("daiv.activity")


class Command(BaseCommand):
    help = "Re-sync non-terminal Activity rows from their linked DBTaskResult."

    def handle(self, *args, **options):
        qs = (
            Activity.objects
            .filter(task_result__isnull=False)
            .exclude(status__in=list(ActivityStatus.terminal()))
            .select_related("task_result", "scheduled_job")
        )

        synced = skipped = errored = 0
        for activity in qs.iterator():
            try:
                if activity.sync_and_save():
                    synced += 1
                else:
                    skipped += 1
            except Exception:
                errored += 1
                logger.exception("Failed to sync activity %s", activity.id)

        summary = f"Synced: {synced}, already up to date: {skipped}, errored: {errored}"
        if errored:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
