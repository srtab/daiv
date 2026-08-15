from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from django.db.models import Count, Min, Q
from django.utils import timezone

from asgiref.sync import sync_to_async
from crontask import cron
from django_tasks import task

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from core.utils import locked_task
from memory.consolidation import document_would_be_discarded, run_consolidation_round
from memory.models import MemoryObservation, RepositoryMemory

logger = logging.getLogger("daiv.memory")


@task()
@locked_task(key="{repo_id}")
async def consolidate_memory_task(repo_id: str) -> None:
    """Fold pending observations into the repository's memory entries ("dreaming").

    The model decides per observation — add a fact, correct or merge existing entries, confirm a
    duplicate, discard noise — and only the entries an operation names are rewritten. The document
    is then re-rendered from the surviving entries; budget pressure can additionally retire
    entries no operation mentioned. Failures propagate to django-tasks (logged + marked failed);
    agent runs are never affected — this runs out-of-band.

    Throttling is the caller's job (see ``consolidate_memory_cron_task``). Serialised per
    repository by ``locked_task`` rather than django-tasks' ``dedup``: a round's claim bookkeeping
    is per-process, so two overlapping rounds would both consume the same pending observations and
    orphan each other's supersede links. A trigger that loses the lock leaves its observations
    pending for the holder or the next cron sweep.
    """
    if not site_settings.memory_enabled:
        logger.info("consolidate_memory_task: memory disabled site-wide, skipping repo %s", repo_id)
        return

    config = await asyncio.to_thread(RepositoryConfig.get_config, repo_id)
    if not config.memory.enabled:
        logger.info("consolidate_memory_task: memory disabled for repo %s, skipping", repo_id)
        return

    observations = [
        obs async for obs in MemoryObservation.objects.filter(repo_id=repo_id).pending().order_by("created_at")
    ]
    if not observations:
        logger.info("consolidate_memory_task: no pending observations for repo %s, skipping", repo_id)
        return

    try:
        if await sync_to_async(document_would_be_discarded)(repo_id):
            logger.error(
                "consolidate_memory_task: repo %s has a memory document but no entries, so re-rendering would "
                "discard it; run `backfill_memory_entries --repo-id %s` first. Leaving %d observation(s) pending.",
                repo_id,
                repo_id,
                len(observations),
            )
            return
        outcome = await run_consolidation_round(repo_id, config, observations)
    finally:
        # In ``finally`` because the cron reads this: a round that raises without recording an
        # attempt is due again on the next hourly sweep, and pays for a full LLM round each time.
        await RepositoryMemory.objects.aupdate_or_create(
            repo_id=repo_id, defaults={"last_attempted_at": timezone.now()}
        )

    if outcome is None:
        return
    if unnamed := outcome.still_pending - outcome.truncated:
        logger.error(
            "consolidate_memory_task: repo %s — the model left %d of %d observation(s) unclaimed; they stay "
            "pending and will be retried, which never converges if the model keeps skipping them",
            repo_id,
            unnamed,
            len(observations),
        )
    if outcome.truncated:
        # Not an error: unlike an observation the model skipped, this one is deferred by our own
        # cap and the next round makes progress on it.
        logger.warning(
            "consolidate_memory_task: repo %s — deferred %d of %d observation(s) past the operation cap; "
            "the next round picks them up",
            repo_id,
            outcome.truncated,
            len(observations),
        )
    logger.info(
        "consolidate_memory_task: repo %s — applied %d operation(s) (%d consolidated, %d discarded, "
        "%d still pending, %d rejected)",
        repo_id,
        outcome.applied,
        outcome.consolidated,
        outcome.discarded,
        outcome.still_pending,
        outcome.rejected,
    )


@task(dedup=True)
async def extract_observations_task(run_id: str) -> None:
    """Extract candidate memory observations from a finished run's transcript.

    Transcripts live in the Redis checkpointer behind a TTL, so this must run promptly after the
    run finishes; an expired checkpoint is a silent skip.

    ``dedup=True`` is keyed on the unique ``run_id``: a duplicate ``run_finished`` delivery for the
    same run is suppressed (no double observations), while a different run always re-runs.
    (Consolidation, keyed on the reusable ``repo_id``, must NOT dedup — see
    ``consolidate_memory_task``.)

    Precondition failures (missing run, disabled flag, expired checkpoint, unconfigured model) are
    log + return — never an error confused with a run failure. See ``memory.extraction`` for the
    pipeline's own failure contract.
    """
    from sessions.models import Run

    from memory.extraction import extract_observations

    if not site_settings.memory_enabled:
        logger.info("extract_observations_task: memory disabled site-wide, skipping run %s", run_id)
        return

    run = await Run.objects.filter(pk=run_id).afirst()
    if run is None:
        logger.warning("extract_observations_task: run %s not found, skipping", run_id)
        return
    if not run.session_id:
        logger.warning(
            "extract_observations_task: run %s has no session_id (violates thread_id contract), skipping", run_id
        )
        return

    config = await asyncio.to_thread(RepositoryConfig.get_config, run.repo_id)
    if not config.memory.enabled:
        logger.info("extract_observations_task: memory disabled for repo %s, skipping", run.repo_id)
        return

    if not (observations := await extract_observations(run)):
        return

    await MemoryObservation.objects.abulk_create([
        MemoryObservation(repo_id=run.repo_id, run=run, category=obs.category, content=obs.content)
        for obs in observations
    ])
    logger.info(
        "extract_observations_task: stored %d observations for repo %s (run=%s)", len(observations), run.repo_id, run_id
    )


# Hourly is fine-grained relative to the per-repo interval cooldown (default 24h, the real
# throttle): the sweep only controls how soon after a repo crosses the threshold it is picked
# up. Hardcoded like the other housekeeping crons (see core.tasks.prune_db_task_results_cron_task)
# rather than added to the DAIV_MEMORY_* site settings, which resolve at runtime and so can't feed
# the import-time @cron schedule.
@cron("0 * * * *")
@task
async def consolidate_memory_cron_task() -> None:
    """Sweep every repository and enqueue consolidation for those that are due.

    This is the sole automatic scheduler for consolidation ("dreaming"); the
    ``consolidate_memory`` management command is the only other entry point and runs
    in-process for an operator, not on a schedule. Unlike the former extraction-time
    trigger, this also sweeps repos that have gone quiet (no recent runs), so accumulated
    observations never sit unconsolidated indefinitely.

    A repo is due when it has at least ``memory_consolidation_min_pending`` pending
    observations **or** its oldest pending observation is older than
    ``memory_consolidation_max_pending_age_days`` (so a low-volume repo still forms memory
    eventually), and its last *attempt* is older than
    ``memory_consolidation_min_interval_hours`` (or it never ran). Gating on the attempt
    rather than the last successful consolidation gives a repo whose rounds keep failing the
    same backoff as a healthy one. All thresholds come from ``site_settings``. The actual
    work — and the per-repo ``.daiv.yml`` flag check — stays in ``consolidate_memory_task``,
    which re-reads pending and no-ops if empty, so a repo disabled or drained between sweep
    and run is handled there.
    """
    if not site_settings.memory_enabled:
        logger.info("consolidate_memory_cron_task: memory disabled site-wide, skipping sweep")
        return

    now = timezone.now()
    cutoff = now - timedelta(hours=site_settings.memory_consolidation_min_interval_hours)
    age_cutoff = now - timedelta(days=site_settings.memory_consolidation_max_pending_age_days)
    due_repo_ids = [
        repo_id
        async for repo_id in (
            MemoryObservation.objects
            .pending()
            .values("repo_id")
            .annotate(pending=Count("pk"), oldest_pending=Min("created_at"))
            .filter(Q(pending__gte=site_settings.memory_consolidation_min_pending) | Q(oldest_pending__lt=age_cutoff))
            .values_list("repo_id", flat=True)
        )
    ]
    # One batched lookup for the cooldown gate instead of a per-repo query: repos attempted
    # within the interval are skipped. Repos with no memory row (or a null last_attempted_at)
    # are absent here, so they correctly stay due.
    recently_attempted = {
        repo_id
        async for repo_id in RepositoryMemory.objects.filter(
            repo_id__in=due_repo_ids, last_attempted_at__gt=cutoff
        ).values_list("repo_id", flat=True)
    }

    enqueued = failed = 0
    for repo_id in due_repo_ids:
        if repo_id in recently_attempted:
            continue
        # Isolate each repo: a per-repo enqueue error (``aenqueue`` is a real INSERT under the
        # deduplicating backend) must not abort the sweep and starve the remaining repos —
        # same catch-log-continue contract as ``dispatch_scheduled_jobs_cron_task``.
        try:
            await consolidate_memory_task.aenqueue(repo_id)
            enqueued += 1
        except Exception:
            logger.exception(
                "consolidate_memory_cron_task: failed to enqueue consolidation for repo %s, skipping", repo_id
            )
            failed += 1

    if enqueued:
        logger.info("consolidate_memory_cron_task: enqueued consolidation for %d repo(s)", enqueued)
    if failed:
        logger.warning("consolidate_memory_cron_task: %d repo(s) failed to enqueue", failed)
