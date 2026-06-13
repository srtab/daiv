from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.utils import timezone

from django_tasks import task
from langchain_core.messages import HumanMessage, SystemMessage

from automation.agent.base import BaseAgent
from automation.agent.constants import ModelName
from codebase.repo_config import RepositoryConfig
from memory.models import MemoryObservation, ObservationStatus, RepositoryMemory
from memory.prompts import consolidation_human, consolidation_system
from memory.schemas import ConsolidatedMemory

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
    except RuntimeError:
        logger.exception("consolidate_memory_task: model not configured for repo %s, skipping", repo_id)
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

    memory.content = enforce_memory_budget(result.content.strip())
    memory.last_consolidated_at = timezone.now()
    await memory.asave(update_fields=["content", "last_consolidated_at", "updated_at"])
    await MemoryObservation.objects.filter(pk__in=[obs.pk for obs in observations]).aupdate(
        status=ObservationStatus.CONSOLIDATED
    )
    logger.info("consolidate_memory_task: consolidated %d observations for repo %s", len(observations), repo_id)
