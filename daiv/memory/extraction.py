from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from core.site_settings import site_settings
from memory.llm import build_structured_llm
from memory.schemas import ExtractedObservations

if TYPE_CHECKING:
    from sessions.models import Run

    from memory.schemas import ExtractedObservation

logger = logging.getLogger("daiv.memory")


async def extract_observations(run: Run) -> list[ExtractedObservation]:
    """Extract candidate memory observations from a finished run's transcript.

    Returns an empty list both when the run taught nothing and when a precondition makes
    extraction impossible — an expired checkpoint, a checkpoint with no messages, a transcript
    with no AI turns, or no configured extraction model. Each of those logs its own reason at its
    own level, so the caller does not need to tell them apart: neither outcome persists anything.

    The LLM ``ainvoke`` is deliberately NOT guarded: a schema mismatch must surface loudly, and a
    transient failure marks the calling task FAILED (no retry; the checkpoint TTLs out) — i.e.
    that one run's observations are lost. Losing a single run's learnings is an accepted
    trade-off; agent runs are unaffected because this runs out-of-band.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from core.checkpointer import aresolve_thread_messages, open_checkpointer
    from memory.prompts import extraction_human, extraction_system
    from memory.transcript import serialize_transcript

    thread_config = {"configurable": {"thread_id": str(run.session_id)}}
    async with open_checkpointer() as checkpointer:
        checkpoint_tuple = await checkpointer.aget_tuple(thread_config)
        if checkpoint_tuple is None:
            # Benign: the checkpoint expired from Redis before this task ran.
            logger.info(
                "extract_observations: checkpoint missing/expired for thread %s (run=%s), skipping",
                run.session_id,
                run.pk,
            )
            return []
        channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {})
        # ``messages`` is stored in a deepagents ``DeltaChannel`` and is usually absent from
        # ``channel_values`` — reconstruct it from the delta write history.
        messages = await aresolve_thread_messages(checkpointer, thread_config, channel_values)

    if not messages:
        # A present checkpoint with no messages even after DeltaChannel reconstruction signals
        # a real defect (serialization or channel-name drift), not normal TTL expiry — louder.
        logger.warning(
            "extract_observations: checkpoint present but has no messages for thread %s (run=%s); "
            "available channels: %s — skipping (serialization or channel-name drift?)",
            run.session_id,
            run.pk,
            sorted(channel_values),
        )
        return []

    if not any(getattr(message, "type", None) == "ai" for message in messages):
        # No agent turns means no agent behaviour to learn from (e.g. the sandbox never came up),
        # so skip the model call rather than pay for an almost certainly empty extraction.
        logger.info(
            "extract_observations: run %s has no AI turns (%d message(s)), nothing to extract, skipping",
            run.pk,
            len(messages),
        )
        return []

    transcript = serialize_transcript(messages)

    extraction_models = tuple(
        model
        for model in (site_settings.memory_extraction_model_name, site_settings.memory_extraction_fallback_model_name)
        if model
    )
    if not extraction_models:
        # Both the model and its fallback resolved to empty (only reachable via an explicit
        # empty-string env override, e.g. DAIV_MEMORY_EXTRACTION_MODEL_NAME=""). Treat it as
        # the documented precondition-failure skip rather than letting build_structured_llm
        # raise IndexError on model_names[0], which would crash the task with no breadcrumb.
        logger.error(
            "extract_observations: no extraction model configured "
            "(check DAIV_MEMORY_EXTRACTION_MODEL_NAME / _FALLBACK_MODEL_NAME), skipping run %s",
            run.pk,
        )
        return []
    try:
        structured_llm = build_structured_llm(ExtractedObservations, extraction_models)
    except RuntimeError, ValueError:
        # Same precondition-failure handling as consolidation: a misconfigured/unparseable
        # extraction model spec is a clean skip, not a task crash.
        logger.exception("extract_observations: extraction model unavailable/misconfigured, skipping")
        return []

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

    return list(result.observations) if result else []
