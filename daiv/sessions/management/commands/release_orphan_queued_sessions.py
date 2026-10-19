from __future__ import annotations

from django.core.management.base import BaseCommand

from sessions.queue import release_orphan_queued_runs


class Command(BaseCommand):
    help = (
        "Release QUEUED Runs whose session has no active (READY/RUNNING) sibling. "
        "Mitigates a rare TOCTOU loss where the dispatcher missed a terminal transition "
        "or the row was created QUEUED but never picked up."
    )

    def handle(self, *args, **options):
        result = release_orphan_queued_runs()
        summary = f"Released: {result.released}, skipped: {result.skipped}, errored: {result.errored}"
        if result.errored:
            self.stdout.write(self.style.WARNING(f"{summary} — see logs; broker may be unavailable."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
