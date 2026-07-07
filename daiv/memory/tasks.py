from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from asgiref.sync import sync_to_async
from crontask import cron
from django_tasks import task
from langchain_core.messages import HumanMessage, SystemMessage

from automation.agent.base import BaseAgent
from codebase.repo_config import RepositoryConfig
from core.checkpointer import open_checkpointer
from core.site_settings import site_settings
from memory.models import MemoryObservation, ObservationStatus, RepositoryMemory
from memory.prompts import consolidation_human, consolidation_system, extraction_human, extraction_system
from memory.schemas import ConsolidatedMemory, ExtractedObservations
from memory.transcript import serialize_transcript

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("daiv.memory")

# Documented defaults for the memory knobs. The live values are served by ``site_settings``
# (env var > site-configuration UI > default); these constants mirror the defaults declared
# in ``core.site_settings._build_field_defaults`` (parity-tested in test_consolidation_task).
# ``MEMORY_MAX_LINES``/``MEMORY_MAX_BYTES`` also double as the safety-net defaults for
# ``enforce_memory_budget``; ``CONSOLIDATION_MIN_PENDING`` is also the threshold the
# ``consolidate_memory`` management command enforces.
CONSOLIDATION_MIN_PENDING = 10
MEMORY_MAX_LINES = 200
MEMORY_MAX_BYTES = 10_240


def _build_structured_llm(schema: type, model_names: Sequence[str]):
    """Structured-output chain with retry + model fallbacks (same pattern as titling).

    No ``max_tokens`` cap: reasoning models count reasoning tokens toward the budget,
    so a tight cap starves the structured-output JSON.
    """

    def _structured(model_name: str):
        return BaseAgent.get_model(model=model_name).with_structured_output(schema).with_retry(stop_after_attempt=2)

    chain = _structured(model_names[0])
    if fallbacks := [_structured(name) for name in model_names[1:]]:
        chain = chain.with_fallbacks(fallbacks)
    return chain


def enforce_memory_budget(content: str, *, max_lines: int = MEMORY_MAX_LINES, max_bytes: int = MEMORY_MAX_BYTES) -> str:
    """Hard safety net for the prompt-stated budget: truncate if the model overshot."""
    lines = content.splitlines()
    if len(lines) > max_lines:
        content = "\n".join(lines[:max_lines])
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return content


@task()
async def consolidate_memory_task(repo_id: str) -> None:
    """Rewrite the repository memory document from pending observations ("dreaming").

    Merges duplicates, resolves contradictions (newest wins), prunes stale items and
    generalizes recurring observations. Failures propagate to django-tasks (logged +
    marked failed); agent runs are never affected — this runs out-of-band.

    Throttling is the caller's job (see ``consolidate_memory_cron_task``); this is not
    deduplicated, so a redundant trigger simply finds 0 pending observations and no-ops.
    """
    if not site_settings.memory_enabled:
        logger.info("consolidate_memory_task: memory disabled site-wide, skipping repo %s", repo_id)
        return

    config = await asyncio.to_thread(RepositoryConfig.get_config, repo_id)
    if not config.memory.enabled:
        logger.info("consolidate_memory_task: memory disabled for repo %s, skipping", repo_id)
        return

    observations = [
        obs
        async for obs in MemoryObservation.objects.filter(repo_id=repo_id, status=ObservationStatus.PENDING).order_by(
            "created_at"
        )
    ]
    if not observations:
        logger.info("consolidate_memory_task: no pending observations for repo %s, skipping", repo_id)
        return

    # Empty override → reuse the repo's agent model (it rewrites the whole document, quality matters).
    consolidation_model = site_settings.memory_consolidation_model_name or config.models.agent.model
    try:
        structured_llm = _build_structured_llm(
            ConsolidatedMemory, (consolidation_model, config.models.agent.fallback_model)
        )
    except RuntimeError, ValueError:
        # RuntimeError: provider disabled / no API key / unknown provider_type.
        # ValueError: empty or unparseable model spec / no matching provider row.
        # Both are precondition failures, not crashes — skip silently like every other.
        logger.exception("consolidate_memory_task: model unavailable/misconfigured for repo %s, skipping", repo_id)
        return

    memory, _created = await RepositoryMemory.objects.aget_or_create(repo_id=repo_id)

    max_lines = site_settings.memory_max_lines
    max_bytes = site_settings.memory_max_bytes

    observations_text = "\n".join(
        f"- [{obs.category}] ({obs.created_at:%Y-%m-%d}) {obs.content}" for obs in observations
    )
    system_content = cast("str", consolidation_system.format(max_lines=max_lines, max_bytes=max_bytes).content)
    result = cast(
        "ConsolidatedMemory",
        await structured_llm.with_config(
            run_name="MemoryConsolidation", tags=["MemoryConsolidation"], metadata={"repo_id": repo_id}
        ).ainvoke([
            SystemMessage(content=system_content),
            HumanMessage(
                content=cast(
                    "str",
                    consolidation_human.format(
                        repo_id=repo_id, current_memory=memory.content, observations=observations_text
                    ).content,
                )
            ),
        ]),
    )

    consolidated = enforce_memory_budget(result.content.strip(), max_lines=max_lines, max_bytes=max_bytes)
    if not consolidated:
        # Never let a degenerate (empty/whitespace) LLM response wipe the accumulated
        # document. Keep the existing memory and leave observations pending to retry.
        logger.error(
            "consolidate_memory_task: LLM returned empty/whitespace content for repo %s (raw len=%d, starts=%r); "
            "keeping existing memory and leaving %d observations pending",
            repo_id,
            len(result.content),
            result.content[:80],
            len(observations),
        )
        return

    @sync_to_async
    def _persist() -> None:
        # Content write and status flip must commit together: a crash between them would
        # otherwise orphan observations as CONSOLIDATED against a stale/un-updated document.
        with transaction.atomic():
            memory.content = consolidated
            memory.last_consolidated_at = timezone.now()
            memory.save(update_fields=["content", "last_consolidated_at", "updated_at"])
            MemoryObservation.objects.filter(pk__in=[obs.pk for obs in observations]).update(
                status=ObservationStatus.CONSOLIDATED
            )

    await _persist()
    logger.info("consolidate_memory_task: consolidated %d observations for repo %s", len(observations), repo_id)


@task(dedup=True)
async def extract_observations_task(run_id: str) -> None:
    """Extract candidate memory observations from a finished run's transcript.

    Transcripts live in the Redis checkpointer behind a TTL, so this must run
    promptly after the run finishes; an expired checkpoint is a silent skip.

    ``dedup=True`` is keyed on the unique ``run_id``: a duplicate
    ``run_finished`` delivery for the same run is suppressed (no double
    observations), while a different run always re-runs. (Consolidation, keyed on
    the reusable ``repo_id``, must NOT dedup — see ``consolidate_memory_task``.)

    Precondition failures (missing run, disabled flag, expired checkpoint,
    unconfigured model) are log + return — never an error confused with a run
    failure. The LLM ``ainvoke`` itself is deliberately NOT guarded: a schema
    mismatch must surface loudly, and a transient failure marks this task FAILED
    (no retry; the checkpoint TTLs out) — i.e. that one run's observations are
    lost. Losing a single run's learnings is an accepted trade-off; agent runs
    are unaffected because this runs out-of-band.
    """
    from sessions.models import Run

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

    async with open_checkpointer() as checkpointer:
        checkpoint_tuple = await checkpointer.aget_tuple({"configurable": {"thread_id": str(run.session_id)}})

    channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {}) if checkpoint_tuple else {}
    if not (messages := channel_values.get("messages", [])):
        if checkpoint_tuple is None:
            # Benign: the checkpoint expired from Redis before this task ran.
            logger.info(
                "extract_observations_task: checkpoint missing/expired for thread %s (run=%s), skipping",
                run.session_id,
                run_id,
            )
        else:
            # A present checkpoint with no messages signals a real defect (serialization
            # or channel-name drift), not normal TTL expiry — surface it louder.
            logger.warning(
                "extract_observations_task: checkpoint present but has no messages for thread %s (run=%s); "
                "available channels: %s — skipping (serialization or channel-name drift?)",
                run.session_id,
                run_id,
                sorted(channel_values),
            )
        return

    transcript = serialize_transcript(messages)

    extraction_models = tuple(
        model
        for model in (site_settings.memory_extraction_model_name, site_settings.memory_extraction_fallback_model_name)
        if model
    )
    if not extraction_models:
        # Both the model and its fallback resolved to empty (only reachable via an explicit
        # empty-string env override, e.g. DAIV_MEMORY_EXTRACTION_MODEL_NAME=""). Treat it as
        # the documented precondition-failure skip rather than letting _build_structured_llm
        # raise IndexError on model_names[0], which would crash the task with no breadcrumb.
        logger.error(
            "extract_observations_task: no extraction model configured "
            "(check DAIV_MEMORY_EXTRACTION_MODEL_NAME / _FALLBACK_MODEL_NAME), skipping run %s",
            run_id,
        )
        return
    try:
        structured_llm = _build_structured_llm(ExtractedObservations, extraction_models)
    except RuntimeError, ValueError:
        # Same precondition-failure handling as consolidation: a misconfigured/unparseable
        # extraction model spec is a clean skip, not a task crash.
        logger.exception("extract_observations_task: extraction model unavailable/misconfigured, skipping")
        return

    result = cast(
        "ExtractedObservations",
        await structured_llm.with_config(
            run_name="MemoryExtraction",
            tags=["MemoryExtraction"],
            metadata={"repo_id": run.repo_id, "run_id": str(run.pk)},
        ).ainvoke([
            SystemMessage(content=cast("str", extraction_system.format().content)),
            HumanMessage(
                content=cast(
                    "str",
                    extraction_human.format(repo_id=run.repo_id, status=run.status, transcript=transcript).content,
                )
            ),
        ]),
    )

    if result and result.observations:
        await MemoryObservation.objects.abulk_create([
            MemoryObservation(repo_id=run.repo_id, run=run, category=obs.category, content=obs.content)
            for obs in result.observations
        ])
        logger.info(
            "extract_observations_task: stored %d observations for repo %s (run=%s)",
            len(result.observations),
            run.repo_id,
            run_id,
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
    observations and its last consolidation is older than
    ``memory_consolidation_min_interval_hours`` (or it never ran). Both thresholds come
    from ``site_settings``. The actual work — and the per-repo ``.daiv.yml`` flag check —
    stays in ``consolidate_memory_task``, which re-reads pending and no-ops if empty, so a
    repo disabled or drained between sweep and run is handled there.
    """
    if not site_settings.memory_enabled:
        logger.info("consolidate_memory_cron_task: memory disabled site-wide, skipping sweep")
        return

    cutoff = timezone.now() - timedelta(hours=site_settings.memory_consolidation_min_interval_hours)
    due_repo_ids = [
        repo_id
        async for repo_id in (
            MemoryObservation.objects
            .filter(status=ObservationStatus.PENDING)
            .values("repo_id")
            .annotate(pending=Count("pk"))
            .filter(pending__gte=site_settings.memory_consolidation_min_pending)
            .values_list("repo_id", flat=True)
        )
    ]
    # One batched lookup for the cooldown gate instead of a per-repo query: repos consolidated
    # within the interval are skipped. Repos with no memory row (or a null last_consolidated_at)
    # are absent here, so they correctly stay due.
    recently_consolidated = {
        repo_id
        async for repo_id in RepositoryMemory.objects.filter(
            repo_id__in=due_repo_ids, last_consolidated_at__gt=cutoff
        ).values_list("repo_id", flat=True)
    }

    enqueued = failed = 0
    for repo_id in due_repo_ids:
        if repo_id in recently_consolidated:
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
