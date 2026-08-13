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
        "or the row was created QUEUED but never picked up. Delegated-batch continuation "
        "runs that FAILED before ever starting (dispatch failure, no linked task) are "
        "re-queued first so the same pass can release them."
    )

    def handle(self, *args, **options):
        # A continuation is a batch's one shot at resuming its coordinator
        # (run_one_continuation_per_batch), so a never-started FAILED row must be retried,
        # not abandoned. Rows that actually ran (task_result_id set) or whose task exists
        # but couldn't be linked (link_failed prefix) are not retried — the work happened.
        requeued = Run.objects.filter(
            continuation_of_batch_id__isnull=False,
            status=RunStatus.FAILED,
            task_result_id__isnull=True,
            error_message__startswith="dispatch_failed",
        ).update(status=RunStatus.QUEUED, error_message="", finished_at=None, started_at=None)

        active_sessions = set(
            Run.objects.filter(status__in=[RunStatus.READY, RunStatus.RUNNING]).values_list("session_id", flat=True)
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

        summary = f"Released: {released}, requeued continuations: {requeued}, skipped: {skipped}, errored: {errored}"
        if errored:
            self.stdout.write(self.style.WARNING(f"{summary} — see logs; broker may be unavailable."))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
