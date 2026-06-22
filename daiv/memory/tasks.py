from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async
from django_tasks import task
from langchain_core.messages import HumanMessage, SystemMessage

from automation.agent.base import BaseAgent
from automation.agent.constants import ModelName
from codebase.repo_config import RepositoryConfig
from core.checkpointer import open_checkpointer
from memory.models import MemoryObservation, ObservationStatus, RepositoryMemory
from memory.prompts import consolidation_human, consolidation_system, extraction_human, extraction_system
from memory.schemas import ConsolidatedMemory, ExtractedObservations
from memory.transcript import serialize_transcript

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger("daiv.memory")

# Cheap/fast models for the per-run extraction pass; consolidation uses the
# repo-configured agent model (it rewrites the whole document, quality matters).
EXTRACTION_MODEL_NAMES: tuple[ModelName, ...] = (ModelName.GPT_5_4_MINI, ModelName.CLAUDE_HAIKU_4_5)

CONSOLIDATION_MIN_PENDING = 10
CONSOLIDATION_MIN_INTERVAL = timedelta(hours=24)

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

    Throttling is the caller's job (see ``_maybe_trigger_consolidation``); this is not
    deduplicated, so a redundant trigger simply finds 0 pending observations and no-ops.
    """
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

    try:
        structured_llm = _build_structured_llm(
            ConsolidatedMemory, (config.models.agent.model, config.models.agent.fallback_model)
        )
    except RuntimeError, ValueError:
        # RuntimeError: provider disabled / no API key / unknown provider_type.
        # ValueError: empty or unparseable model spec / no matching provider row.
        # Both are precondition failures, not crashes — skip silently like every other.
        logger.exception("consolidate_memory_task: model unavailable/misconfigured for repo %s, skipping", repo_id)
        return

    memory, _created = await RepositoryMemory.objects.aget_or_create(repo_id=repo_id)

    observations_text = "\n".join(
        f"- [{obs.category}] ({obs.created_at:%Y-%m-%d}) {obs.content}" for obs in observations
    )
    result = cast(
        "ConsolidatedMemory",
        await structured_llm.with_config(
            run_name="MemoryConsolidation", tags=["MemoryConsolidation"], metadata={"repo_id": repo_id}
        ).ainvoke([
            SystemMessage(content=cast("str", consolidation_system.format().content)),
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

    consolidated = enforce_memory_budget(result.content.strip())
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
async def extract_observations_task(activity_id: str) -> None:
    """Extract candidate memory observations from a finished run's transcript.

    Transcripts live in the Redis checkpointer behind a TTL, so this must run
    promptly after the run finishes; an expired checkpoint is a silent skip.

    ``dedup=True`` is keyed on the unique ``activity_id``: a duplicate
    ``activity_finished`` delivery for the same run is suppressed (no double
    observations), while a different run always re-runs. (Consolidation, keyed on
    the reusable ``repo_id``, must NOT dedup — see ``consolidate_memory_task``.)

    Precondition failures (missing activity, disabled flag, expired checkpoint,
    unconfigured model) are log + return — never an error confused with a run
    failure. The LLM ``ainvoke`` itself is deliberately NOT guarded: a schema
    mismatch must surface loudly, and a transient failure marks this task FAILED
    (no retry; the checkpoint TTLs out) — i.e. that one run's observations are
    lost. Losing a single run's learnings is an accepted trade-off; agent runs
    are unaffected because this runs out-of-band.
    """
    from activity.models import Activity

    activity = await Activity.objects.filter(pk=activity_id).afirst()
    if activity is None:
        logger.warning("extract_observations_task: activity %s not found, skipping", activity_id)
        return
    if not activity.thread_id:
        logger.warning(
            "extract_observations_task: activity %s has no thread_id (violates thread_id contract), skipping",
            activity_id,
        )
        return

    config = await asyncio.to_thread(RepositoryConfig.get_config, activity.repo_id)
    if not config.memory.enabled:
        logger.info("extract_observations_task: memory disabled for repo %s, skipping", activity.repo_id)
        return

    async with open_checkpointer() as checkpointer:
        checkpoint_tuple = await checkpointer.aget_tuple({"configurable": {"thread_id": activity.thread_id}})

    channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {}) if checkpoint_tuple else {}
    if not (messages := channel_values.get("messages", [])):
        if checkpoint_tuple is None:
            # Benign: the checkpoint expired from Redis before this task ran.
            logger.info(
                "extract_observations_task: checkpoint missing/expired for thread %s (activity=%s), skipping",
                activity.thread_id,
                activity_id,
            )
        else:
            # A present checkpoint with no messages signals a real defect (serialization
            # or channel-name drift), not normal TTL expiry — surface it louder.
            logger.warning(
                "extract_observations_task: checkpoint present but has no messages for thread %s (activity=%s); "
                "available channels: %s — skipping (serialization or channel-name drift?)",
                activity.thread_id,
                activity_id,
                sorted(channel_values),
            )
        return

    transcript = serialize_transcript(messages)

    try:
        structured_llm = _build_structured_llm(ExtractedObservations, EXTRACTION_MODEL_NAMES)
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
            metadata={"repo_id": activity.repo_id, "activity_id": str(activity.pk)},
        ).ainvoke([
            SystemMessage(content=cast("str", extraction_system.format().content)),
            HumanMessage(
                content=cast(
                    "str",
                    extraction_human.format(
                        repo_id=activity.repo_id, status=activity.status, transcript=transcript
                    ).content,
                )
            ),
        ]),
    )

    if result and result.observations:
        await MemoryObservation.objects.abulk_create([
            MemoryObservation(repo_id=activity.repo_id, activity=activity, category=obs.category, content=obs.content)
            for obs in result.observations
        ])
        logger.info(
            "extract_observations_task: stored %d observations for repo %s (activity=%s)",
            len(result.observations),
            activity.repo_id,
            activity_id,
        )

    await _maybe_trigger_consolidation(activity.repo_id)


async def _maybe_trigger_consolidation(repo_id: str) -> None:
    """Piggyback consolidation scheduling on extraction (no cron exists).

    Enqueue when the repo accumulated >= CONSOLIDATION_MIN_PENDING observations and
    the last consolidation is older than CONSOLIDATION_MIN_INTERVAL (or never ran).
    """
    pending = await MemoryObservation.objects.filter(repo_id=repo_id, status=ObservationStatus.PENDING).acount()
    if pending < CONSOLIDATION_MIN_PENDING:
        return
    memory = await RepositoryMemory.objects.filter(repo_id=repo_id).afirst()
    if (
        memory
        and memory.last_consolidated_at
        and timezone.now() - memory.last_consolidated_at < CONSOLIDATION_MIN_INTERVAL
    ):
        return
    await consolidate_memory_task.aenqueue(repo_id)
    logger.info("_maybe_trigger_consolidation: enqueued consolidation for repo %s (%d pending)", repo_id, pending)
