from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.db.models import Q

from sessions.models import Run, RunStatus
from sessions.signals import _enqueue_queued_run

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = (
        "Release QUEUED Runs whose session has no active (READY/RUNNING) sibling. "
        "Mitigates a rare TOCTOU loss where the dispatcher missed a terminal transition "
        "or the row was created QUEUED but never picked up."
    )

    def handle(self, *args, **options):
        active_sessions = set(
            Run.objects.filter(status__in=[RunStatus.READY, RunStatus.RUNNING], session_id__isnull=False).values_list(
                "session_id", flat=True
            )
        )

        orphans = (
            Run.objects
            .filter(status=RunStatus.QUEUED)
            .filter(~Q(session_id__in=active_sessions))
            .order_by("session_id", "created_at")
        )

        seen_sessions: set[str] = set()
        released = skipped = errored = 0
        for run in orphans.iterator():
            if run.session_id in seen_sessions:
                skipped += 1
                continue
            seen_sessions.add(run.session_id)
            try:
                claimed = Run.objects.filter(pk=run.pk, status=RunStatus.QUEUED).update(status=RunStatus.READY)
            except IntegrityError:
                # A concurrent submission claimed the session between our snapshot of
                # active_sessions and this CAS; leave the row QUEUED for a future pass.
                skipped += 1
                continue
            if claimed != 1:
                skipped += 1
                continue
            run.refresh_from_db()
            try:
                ok = _enqueue_queued_run(run)
            except Exception:
                errored += 1
                logger.exception("Failed to release orphan QUEUED run %s", run.pk)
                continue
            if ok:
                released += 1
            else:
                errored += 1

        summary = f"Released: {released}, skipped: {skipped}, errored: {errored}"
        if errored:
            self.stdout.write(self.style.WARNING(f"{summary} — see logs; broker may be unavailable."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
