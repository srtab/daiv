from __future__ import annotations

import logging

from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.db.models import Exists, OuterRef

from sessions.models import Run, RunStatus
from sessions.signals import DISPATCH_FAILED_PREFIX, _enqueue_queued_run

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = (
        "Release QUEUED Runs whose session has no active (READY/RUNNING) sibling. "
        "Mitigates a rare TOCTOU loss where the dispatcher missed a terminal transition "
        "or the row was created QUEUED but never picked up. Delegated-batch continuation "
        "runs that FAILED before ever starting (dispatch failure, no linked task) are "
        "re-queued first so the same pass can release them."
    )

    def handle(self, *args, **options):
        # A continuation is a batch's one shot at resuming its coordinator
        # (run_one_continuation_per_batch), so a never-started FAILED row must be retried.
        # A row that reached the broker (task_result_id set, or the link_failed prefix) is not.
        requeued = Run.objects.filter(
            continuation_of_batch_id__isnull=False,
            status=RunStatus.FAILED,
            task_result_id__isnull=True,
            error_message__startswith=DISPATCH_FAILED_PREFIX,
        ).update(status=RunStatus.QUEUED, error_message="", finished_at=None, started_at=None)

        orphans = (
            Run.objects
            .filter(status=RunStatus.QUEUED)
            .exclude(
                Exists(
                    Run.objects.filter(
                        session_id=OuterRef("session_id"), status__in=[RunStatus.READY, RunStatus.RUNNING]
                    )
                )
            )
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
                # A concurrent submission claimed the session between the orphan scan and
                # this CAS; leave the row QUEUED for a future pass.
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

        summary = f"Released: {released}, requeued continuations: {requeued}, skipped: {skipped}, errored: {errored}"
        if errored:
            self.stdout.write(self.style.WARNING(f"{summary} — see logs; broker may be unavailable."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
