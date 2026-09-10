from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from core.site_settings import site_settings
from memory.llm import build_structured_llm
from memory.schemas import CONTENT_HARD_LIMIT, ExtractedObservations

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sessions.models import Run

    from memory.schemas import ExtractedObservation

logger = logging.getLogger("daiv.memory")

# How much of a lost observation the ERROR below records. Its own figure rather than the prompt's
# guideline: retuning editorial guidance must not shorten the only trace of an unrecoverable loss.
LOG_EXCERPT_CHARS = 500


def _usable(observations: Sequence[ExtractedObservation], *, run_ref: str) -> list[ExtractedObservation]:
    """The observations worth storing.

    ``run_ref`` labels the two logs below with something traceable: the run's pk from the task
    path, the case id from the eval. Applies no batch cap on purpose: an observation dropped here
    is never persisted, so nothing can re-queue it, whereas ``MAX_OPERATIONS`` in consolidation
    defers its tail instead of destroying it.
    """
    kept: list[ExtractedObservation] = []
    unusable: list[str] = []
    runaway: list[str] = []
    for observation in observations:
        # Length first so the two buckets read one source: bucketing on ``shape_error()``'s message
        # would silently file any future rule under the WARNING below.
        if len(observation.content) > CONTENT_HARD_LIMIT:
            runaway.append(observation.content[:LOG_EXCERPT_CHARS])
        elif reason := observation.shape_error():
            unusable.append(reason)
        else:
            kept.append(observation)

    if unusable:
        logger.warning(
            "extract_observations: dropped %d of %d observation(s) from %s as unusable: %s",
            len(unusable),
            len(observations),
            run_ref,
            unusable,
        )
    if runaway:
        # ERROR, not warning: these are dropped before any row exists, so nothing re-queues them
        # and the transcript TTLs out — this log is the only trace of a real fact that was lost.
        logger.error(
            "extract_observations: dropped %d of %d observation(s) from %s as over-long, unrecoverably; "
            "first %d characters of each: %s",
            len(runaway),
            len(observations),
            run_ref,
            LOG_EXCERPT_CHARS,
            runaway,
        )
    return kept


async def extract_from_transcript(
    transcript: str,
    *,
    repo_id: str,
    status: str,
    memory: str = "",
    model_names: Sequence[str] | None = None,
    run_ref: str | None = None,
) -> list[ExtractedObservation]:
    """Run the extraction model over an already-serialized transcript.

    The half of extraction that has no Django or Redis dependency: given text, it returns the
    usable observations. Callers own transcript sourcing. ``model_names`` defaults to the
    site-settings pair; an empty resolution is the documented precondition-failure skip, not a
    crash on ``model_names[0]``. ``run_ref`` labels the drop logs — the run's pk from the task
    path, the case id from the eval. ``memory`` is the repository's current memory document,
    shown to the model so it can tell a read-only restatement from a contradiction or
    re-verification (see the prompt's three-way rule).
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from memory.prompts import extraction_human, extraction_system

    run_ref = run_ref or repo_id
    if model_names is None:
        model_names = tuple(
            model
            for model in (
                site_settings.memory_extraction_model_name,
                site_settings.memory_extraction_fallback_model_name,
            )
            if model
        )
    if not model_names:
        logger.error(
            "extract_observations: no extraction model configured "
            "(check DAIV_MEMORY_EXTRACTION_MODEL_NAME / _FALLBACK_MODEL_NAME), skipping %s",
            run_ref,
        )
        return []
    try:
        structured_llm = build_structured_llm(ExtractedObservations, model_names)
    except RuntimeError, ValueError:
        logger.exception("extract_observations: extraction model unavailable/misconfigured, skipping")
        return []

    result = cast(
        "ExtractedObservations",
        await structured_llm.with_config(
            run_name="MemoryExtraction", tags=["MemoryExtraction"], metadata={"repo_id": repo_id, "run_ref": run_ref}
        ).ainvoke([
            SystemMessage(content=cast("str", extraction_system.format().content)),
            HumanMessage(
                content=cast(
                    "str",
                    extraction_human.format(
                        repo_id=repo_id, status=status, memory=memory, transcript=transcript
                    ).content,
                )
            ),
        ]),
    )

    return _usable(result.observations, run_ref=run_ref) if result else []


async def extract_observations(run: Run) -> list[ExtractedObservation]:
    """Extract candidate memory observations from a finished run's transcript.

    Returns an empty list both when the run taught nothing and when a precondition makes
    extraction impossible — an expired checkpoint, a checkpoint with no messages, a transcript
    with no AI turns, or no configured extraction model. Each of those logs its own reason at its
    own level, so the caller does not need to tell them apart: neither outcome persists anything.

    The LLM ``ainvoke`` is deliberately NOT guarded: a schema mismatch must surface loudly, and a
    transient failure marks the calling task FAILED (no retry; the checkpoint TTLs out) — i.e.
    that one run's observations are lost. Losing a single run's learnings is an accepted
    trade-off; agent runs are unaffected because this runs out-of-band. Unusable *individual*
    observations are not a schema mismatch and never reach that path — see ``_usable``.

    Model resolution and the LLM call now live in ``extract_from_transcript``; this half only
    loads the transcript out of the checkpoint. It also loads the repository's current memory
    document and passes it along, so a fact the run merely read is not re-emitted, while a
    contradiction or a re-verification still is.

    That document is re-read here, at extraction time, while the run itself saw whatever
    ``RepositoryMemoryMiddleware`` snapshotted at its own start — a consolidation round in
    between can make the two differ. Accepted skew, not fixed here.
    """
    from core.checkpointer import aresolve_thread_messages, open_checkpointer
    from memory.models import RepositoryMemory
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

    try:
        memory = (
            await RepositoryMemory.objects.filter(repo_id=run.repo_id).values_list("content", flat=True).afirst() or ""
        ).strip()
    except Exception:
        # Falls open to "" on any lookup failure: an extraction that runs blind is worse than
        # none, but not by much, and must never block the run.
        logger.exception("extract_observations: could not load memory for repo %s, continuing without it", run.repo_id)
        memory = ""

    return await extract_from_transcript(
        transcript, repo_id=run.repo_id, status=run.status, memory=memory, run_ref=str(run.pk)
    )
