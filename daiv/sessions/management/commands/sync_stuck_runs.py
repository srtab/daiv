from __future__ import annotations

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.tasks import TaskResultStatus
from django.utils import timezone

from django_tasks_db.models import DBTaskResult

from core import worker_liveness
from sessions.executor.lock import DEAD_RUN_MESSAGE, DeadWorkerError, stale_cutoff
from sessions.managers import fail_tasks
from sessions.models import Run, RunStatus, Session, SessionOrigin
from sessions.queue import ENQUEUE_FAILED_PREFIX

logger = logging.getLogger("daiv.sessions")

NEVER_QUEUED_AFTER = timedelta(minutes=10)


class Command(BaseCommand):
    help = (
        "Reconcile non-terminal Run rows. Task-backed runs are re-synced from their linked "
        "DBTaskResult; inline chat runs (which have no task result) that a worker crash left "
        "stuck in RUNNING are failed once their session heartbeat goes stale, task-backed runs a worker crash "
        "left RUNNING are failed the same way along with their tasks, RUNNING tasks whose worker is gone and no "
        "unfinished run stands for are failed, and READY runs whose submitter stopped before queueing their task are "
        "failed once they are old enough."
    )

    def handle(self, *args, **options):
        qs = (
            Run.objects
            .filter(task_result__isnull=False)
            .exclude(status__in=list(RunStatus.terminal()))
            .select_related("task_result", "session", "session__scheduled_job")
        )

        synced = skipped = 0
        errored_ids: list[str] = []
        for run in qs.iterator():
            try:
                if run.sync_and_save():
                    synced += 1
                else:
                    skipped += 1
            except Exception:
                errored_ids.append(str(run.id))
                logger.exception("Failed to sync run %s", run.id)

        reaped = self._reap_orphaned_chat_runs()
        dead = self._reap_dead_task_runs()
        dead_tasks = self._fail_dead_worker_tasks()
        never_queued = self._reap_never_queued_runs()

        summary = (
            f"Synced: {synced}, already up to date: {skipped}, errored: {len(errored_ids)}, "
            f"chat runs reaped: {reaped}, dead task runs reaped: {dead}, dead-worker tasks failed: {dead_tasks}, "
            f"never-queued runs reaped: {never_queued}"
        )
        if errored_ids:
            # Fail the command run — and thus the cron task's DBTaskResult when scheduled — so
            # per-row errors surface to monitoring. Individual failing rows are only counted and
            # logged (above); they are not transitioned to FAILED here. The failed run ids are
            # appended so the failed task record is self-contained. The summary is logged too.
            summary = f"{summary} (failed run ids: {', '.join(errored_ids)})"
            logger.warning(summary)
            raise CommandError(summary)
        logger.info(summary)

    def _reap_orphaned_chat_runs(self) -> int:
        """Fail chat runs orphaned by a hard worker crash.

        Chat turns run inline with no DBTaskResult, so the DBTaskResult sync above can never
        reconcile one whose streamer ``finally`` never ran (OOM / SIGKILL before it could
        finalize). We reap those using the same staleness signal ``SessionLock`` uses to
        decide a holder is dead — the session heartbeat (``last_active_at``), bumped every few
        seconds by a live stream — so a genuinely long-running chat turn is never reaped. The
        finish re-checks that signal, so a stream that heartbeats after the scan keeps its run.
        """
        # A subquery, not a join: Django runs a joined UPDATE as ``id IN (SELECT ...)``, and Postgres does not redo
        # that subquery's status check after waiting on the row lock, so a finish committed meanwhile would be lost.
        stale_sessions = Session.objects.filter(last_active_at__lt=stale_cutoff()).values("pk")
        stale = Q(status=RunStatus.RUNNING, session_id__in=stale_sessions)
        orphans = Run.objects.filter(stale, trigger_type=SessionOrigin.CHAT, task_result__isnull=True)
        reaped = sum(
            Run.objects.finish(
                run,
                RunStatus.FAILED,
                where=stale,
                finished_at=timezone.now(),
                error_message="Orphaned chat run: streamer never finalized (worker crash); reaped by sync_stuck_runs.",
            )
            for run in orphans
        )
        if reaped:
            logger.warning("sync_stuck_runs: reaped %d orphaned chat run(s) stuck in RUNNING", reaped)
        return reaped

    def _reap_dead_task_runs(self) -> int:
        """Fail task-backed runs a hard worker crash left RUNNING, and their tasks (``RunManager.finish_dead``).

        Left alone, a job run keeps its session's one active job place (``run_one_active_per_session``) and a run the
        inbox started never settles the events it took; the finish's ``run_ended`` settles them, frees the slot and
        drains the session (:meth:`sessions.inbox.complete.RunEnd.on_run_ended`).

        Every task-backed run, whatever its trigger, holds its session's slot and heartbeats it
        (:func:`sessions.executor.lock.hold_session_lock`): a job or schedule under its pk, a webhook run under an id
        of its own, a run the inbox started on the slot it claimed. So a run that started before the staleness window
        on a session whose heartbeat went stale is dead; ``started_at`` keeps a run still setting up on an idle session.
        A run whose worker is still alive (:mod:`core.worker_liveness`) is only stalled, and is left to it.
        """
        now = timezone.now()
        cutoff = stale_cutoff(now)
        stale_sessions = Session.objects.filter(last_active_at__lt=cutoff).values("pk")
        dead = Q(status=RunStatus.RUNNING, started_at__lt=cutoff, session_id__in=stale_sessions)
        runs = Run.objects.filter(dead, task_result__isnull=False).select_related("task_result")
        reaped = sum(
            Run.objects.finish_dead(run, where=dead, error=DeadWorkerError, message=DEAD_RUN_MESSAGE, now=now)
            for run in runs
            if not worker_liveness.claimant_alive(run.task_result.worker_ids)
        )
        if reaped:
            logger.warning("sync_stuck_runs: reaped %d dead task-backed run(s) stuck in RUNNING", reaped)
        return reaped

    def _fail_dead_worker_tasks(self) -> int:
        """Fail RUNNING tasks whose worker is gone (:mod:`core.worker_liveness`) that no unfinished run stands for: a
        task no run is linked to, or one a crash kept ``RunManager.finish_dead`` from failing after its run. An
        unfinished run's task is left to :meth:`_reap_dead_task_runs`, which also asks for a stale heartbeat.
        """
        orphaned = Q(run__isnull=True) | Q(run__status__in=list(RunStatus.terminal()))
        running = DBTaskResult.objects.filter(orphaned, status=TaskResultStatus.RUNNING)
        workers = running.order_by().values_list("pk", "worker_ids")
        dead = [pk for pk, worker_ids in workers if not worker_liveness.claimant_alive(worker_ids)]
        failed = fail_tasks(
            running.filter(pk__in=dead),
            error=DeadWorkerError,
            message="Dead task: its worker is gone; failed by sync_stuck_runs.",
            now=timezone.now(),
        )
        if failed:
            logger.warning("sync_stuck_runs: failed %d RUNNING task(s) whose worker is gone", failed)
        return failed

    def _reap_never_queued_runs(self) -> int:
        """Fail READY runs no task was ever queued for, ``NEVER_QUEUED_AFTER`` after they were created.

        A submitter creates a run READY and queues its task straight after (``sessions.queue.aenqueue_run``); one
        cancelled or killed in between leaves a row that never runs and, for an API or MCP thread, holds its session's
        slot so every later run there stays QUEUED. The ``enqueue_failed`` error marks it as one that never ran, and the
        finish releases its session's QUEUED runs.
        """
        now = timezone.now()
        stuck = Q(status=RunStatus.READY, task_result__isnull=True, created_at__lt=now - NEVER_QUEUED_AFTER)
        reaped = sum(
            Run.objects.finish(
                run,
                RunStatus.FAILED,
                where=stuck,
                never_started=True,
                finished_at=now,
                started_at=now,
                error_message=f"{ENQUEUE_FAILED_PREFIX}: its submitter stopped before queueing it; reaped by "
                "sync_stuck_runs.",
            )
            for run in Run.objects.filter(stuck)
        )
        if reaped:
            logger.warning("sync_stuck_runs: reaped %d READY run(s) that were never queued", reaped)
        return reaped
