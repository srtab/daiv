from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.db.models import Q

from activity.models import Activity, ActivityStatus
from activity.signals import _enqueue_queued_activity

logger = logging.getLogger("daiv.activity")


class Command(BaseCommand):
    help = (
        "Release QUEUED Activities whose thread has no active (READY/RUNNING) sibling. "
        "Mitigates a rare TOCTOU loss where the dispatcher missed a terminal transition "
        "or the row was created QUEUED but never picked up."
    )

    def handle(self, *args, **options):
        active_threads = set(
            Activity.objects.filter(
                status__in=[ActivityStatus.READY, ActivityStatus.RUNNING], thread_id__isnull=False
            ).values_list("thread_id", flat=True)
        )

        orphans = (
            Activity.objects
            .filter(status=ActivityStatus.QUEUED)
            .filter(~Q(thread_id__in=active_threads))
            .order_by("thread_id", "created_at")
        )

        seen_threads: set[str] = set()
        released = skipped = errored = 0
        for activity in orphans.iterator():
            if activity.thread_id in seen_threads:
                skipped += 1
                continue
            seen_threads.add(activity.thread_id)
            try:
                claimed = Activity.objects.filter(pk=activity.pk, status=ActivityStatus.QUEUED).update(
                    status=ActivityStatus.READY
                )
            except IntegrityError:
                # A concurrent submission claimed the thread between our snapshot of
                # active_threads and this CAS; leave the row QUEUED for a future pass.
                skipped += 1
                continue
            if claimed != 1:
                skipped += 1
                continue
            activity.refresh_from_db()
            try:
                ok = _enqueue_queued_activity(activity)
            except Exception:
                errored += 1
                logger.exception("Failed to release orphan QUEUED activity %s", activity.pk)
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
