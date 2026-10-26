import logging

from django.core.management import call_command
from django.tasks import task

from crontask import cron

# Alias for one release: rows queued before classification moved still store these ``sessions.tasks`` paths.
from notifications.tasks import classify_run_task, reclassify_missing_envelopes_cron_task  # noqa: F401

from core.constants import TASK_PRIORITY_TITLING, TASK_QUEUE_INTERACTIVE
from core.utils import locked_task

logger = logging.getLogger("daiv.sessions")


# Hardcoded like the other housekeeping crons (see core.tasks.prune_db_task_results_cron_task)
# rather than config-driven: this is a fixed-cadence crash-recovery backstop, and the sessions app
# has no conf.py to feed the import-time @cron schedule.
@cron("*/5 * * * *")
@task
@locked_task(key="sync-stuck-runs")
def sync_stuck_runs_cron_task():
    """Reconcile non-terminal Runs periodically (crash-recovery backstop).

    Re-syncs task-backed runs from their linked DBTaskResult, reaps inline chat runs a
    worker crash left stuck in RUNNING (once the session heartbeat goes stale), and fails READY
    runs whose submitter stopped before queueing them. The normal
    path is the ``run_finished`` signal / streamer ``finally``; this is the safety net for
    missed transitions and hard crashes.

    ``locked_task`` (non-blocking) skips this tick if a prior run still holds the lock, so a
    pass that overruns the interval is never double-dispatched. The wrapped command raises
    ``CommandError`` on per-row failures, which fails this task's DBTaskResult so the error
    is visible to monitoring rather than silently swallowed.
    """
    call_command("sync_stuck_runs")


@cron("*/5 * * * *")
@task
@locked_task(key="resume-stalled-batches")
def resume_stalled_batches_cron_task():
    """Resume the coordinators whose delegated batch finished without a continuation reaching the task queue.

    The normal path is the ``run_finished`` signal (``sessions.delegation.resume_coordinator``); this is its
    backstop. Non-blocking lock, so a sweep that overruns the interval is never double-dispatched.
    """
    from sessions.delegation import resume_stalled_batches

    queued = resume_stalled_batches()
    if queued:
        logger.info("resume_stalled_batches: queued %d continuation(s)", queued)


@task(queue_name=TASK_QUEUE_INTERACTIVE)
async def evaluate_pipeline_watch_task(repo_id: str, ref: str, pipeline_id: int | None = None) -> None:
    """Judge CI for a watched branch and take the one action it implies.

    Short and user-visible, so it runs on the interactive queue rather than behind agent runs.
    """
    from sessions.pipeline_watch.service import PipelineWatch

    await PipelineWatch(repo_id).aevaluate(ref=ref, pipeline_id=pipeline_id)


@cron("*/10 * * * *")
@task
@locked_task(key="reconcile-pipeline-watches")
async def reconcile_pipeline_watches_cron_task():
    """Repair CI watches that webhook events did not resolve.

    Non-blocking lock, so a sweep that overruns the interval is never double-dispatched.
    """
    from sessions.pipeline_watch.reconciler import WatchReconciler

    touched = await WatchReconciler().areconcile()
    logger.info("reconcile_pipeline_watches: touched %d watches", touched)


@task(queue_name=TASK_QUEUE_INTERACTIVE, priority=TASK_PRIORITY_TITLING)
def generate_title_task(pk: str, prompt: str, repo_id: str, ref: str = "") -> None:
    """Overwrite a chat Session's heuristic title with an LLM-generated one.

    Failures propagate to django-tasks (which logs + marks the task failed); the heuristic title set synchronously
    remains.
    """
    from sessions.models import Session
    from sessions.titling import TitlerNotConfiguredError, generate_title

    try:
        session = Session.objects.get(pk=pk)
    except Session.DoesNotExist:
        logger.warning("generate_title_task: session with pk=%s not found, skipping", pk)
        return

    try:
        title = generate_title(
            prompt,
            repo_id=repo_id,
            ref=ref,
            run_metadata={"entity_type": "session", "entity_pk": pk, "repo_id": repo_id, "ref": ref},
        )
    except TitlerNotConfiguredError:
        logger.exception(
            "generate_title_task: model not configured for session pk=%s — feature disabled until API key is set", pk
        )
        return

    session.title = title
    session.save(update_fields=["title"])


@task(queue_name=TASK_QUEUE_INTERACTIVE, priority=TASK_PRIORITY_TITLING)
def generate_batch_title_task(batch_id: str, prompt: str) -> None:
    """Generate a single LLM title for a Run batch and apply it to every untitled member.

    One LLM call per batch instead of one per run. Repo/ref context is omitted because batch
    members typically span repos. Only rows with an empty ``title`` are updated, so synchronous
    titles (e.g. scheduled-run templates) are preserved. The same title is stamped on each
    affected run's Session when the session title is still empty.
    """
    from sessions.models import Run, Session
    from sessions.titling import TitlerNotConfiguredError, generate_title

    try:
        title = generate_title(prompt, run_metadata={"entity_type": "run_batch", "batch_id": batch_id})
    except TitlerNotConfiguredError:
        logger.exception(
            "generate_batch_title_task: model not configured for batch_id=%s — feature disabled until API key is set",
            batch_id,
        )
        return

    updated = Run.objects.filter(batch_id=batch_id, title="").update(title=title)
    # Backfill the parent session title too — a fresh batch creates one session per run,
    # each with an empty title until this titler lands.
    Session.objects.filter(runs__batch_id=batch_id, title="").update(title=title)
    if updated == 0:
        logger.warning(
            "generate_batch_title_task: no rows updated for batch_id=%s (stale batch or all already titled)", batch_id
        )
    else:
        logger.info("generate_batch_title_task: updated %d runs for batch_id=%s", updated, batch_id)
