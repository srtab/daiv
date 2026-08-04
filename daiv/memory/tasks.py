from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.db.models import Count, Min, Q
from django.utils import timezone

from asgiref.sync import sync_to_async
from crontask import cron
from django_tasks import task

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from memory.constants import MEMORY_MAX_BYTES, MEMORY_MAX_LINES
from memory.models import (
    EntryStatus,
    MemoryEntry,
    MemoryObservation,
    ObservationCategory,
    ObservationStatus,
    RepositoryMemory,
)
from memory.schemas import ExtractedObservations, MemoryOperations

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from memory.schemas import MemoryOperation

logger = logging.getLogger("daiv.memory")

# A validated operation paired with the deduplicated targets the apply phase must use.
type AcceptedOperation = tuple[MemoryOperation, list[str], list[MemoryObservation]]

# The only categories the document renders: an entry whose category is absent stays ACTIVE and
# counts against the budget, but appears nowhere. Pinned to the choices by a test.
CATEGORY_SECTIONS: tuple[tuple[str, str], ...] = (
    (ObservationCategory.BUILD_TEST, "## Build & test"),
    (ObservationCategory.CODEBASE_FACT, "## Codebase facts"),
    (ObservationCategory.PITFALL, "## Pitfalls"),
    (ObservationCategory.REVIEWER_PREFERENCE, "## Reviewer preferences"),
    (ObservationCategory.WORKFLOW, "## Workflow"),
)

# Section order doubles as the eviction tie-break between equally large categories.
_SECTION_ORDER = {category: index for index, (category, _header) in enumerate(CATEGORY_SECTIONS)}


def _build_structured_llm(schema: type, model_names: Sequence[str]):
    """Structured-output chain with retry + model fallbacks (same pattern as titling).

    No ``max_tokens`` cap: reasoning models count reasoning tokens toward the budget,
    so a tight cap starves the structured-output JSON.
    """
    from automation.agent.base import BaseAgent

    def _structured(model_name: str):
        return BaseAgent.get_model(model=model_name).with_structured_output(schema).with_retry(stop_after_attempt=2)

    chain = _structured(model_names[0])
    if fallbacks := [_structured(name) for name in model_names[1:]]:
        chain = chain.with_fallbacks(fallbacks)
    return chain


def render_memory_document(entries: Iterable[MemoryEntry]) -> str:
    """Render active entries into the document injected into agent runs.

    Deterministic by construction — fixed section order, entries by creation time then id (a
    round stamps one timestamp on all of its entries), whitespace collapsed so one entry is
    always exactly one line. No model involvement.
    """
    by_category: dict[str, list[MemoryEntry]] = defaultdict(list)
    for entry in entries:
        by_category[entry.category].append(entry)

    sections = []
    for category, header in CATEGORY_SECTIONS:
        if not (items := sorted(by_category.get(category, ()), key=_render_order)):
            continue
        bullets = "\n".join(f"- {' '.join(entry.content.split())}" for entry in items)
        sections.append(f"{header}\n{bullets}")
    return "\n\n".join(sections)


def _render_order(entry: MemoryEntry) -> tuple:
    return (entry.created_at, str(entry.pk))


def _eviction_order(entry: MemoryEntry) -> tuple:
    return (entry.last_confirmed_at, entry.created_at, str(entry.pk))


def document_size(document: str) -> tuple[int, int]:
    """The document's ``(lines, bytes)`` — the two dimensions the render budget is expressed in."""
    return len(document.splitlines()), len(document.encode("utf-8"))


def _fits_budget(document: str, *, max_lines: int, max_bytes: int) -> bool:
    lines, size = document_size(document)
    return lines <= max_lines and size <= max_bytes


def _largest_category(entries: Iterable[MemoryEntry]) -> str:
    content_bytes: Counter[str] = Counter()
    for entry in entries:
        content_bytes[entry.category] += len(entry.content.encode("utf-8"))
    return min(
        content_bytes,
        key=lambda category: (-content_bytes[category], _SECTION_ORDER.get(category, len(_SECTION_ORDER))),
    )


def prune_to_budget(
    entries: Iterable[MemoryEntry], *, max_lines: int = MEMORY_MAX_LINES, max_bytes: int = MEMORY_MAX_BYTES
) -> tuple[list[MemoryEntry], list[MemoryEntry]]:
    """Split entries into the ones that fit the render budget and the ones to evict.

    Pressure-triggered and category-scoped: while the render fits, nothing is evicted; under
    pressure the biggest category gives up its least-recently-confirmed entry, so a small
    category is never drained to make room for a large one.

    The last entry is never evicted. A budget too small to hold even one entry is a
    misconfiguration, and overshooting it by a single bullet beats erasing the repository's
    whole memory.
    """
    kept = list(entries)
    evicted: list[MemoryEntry] = []
    while len(kept) > 1 and not _fits_budget(render_memory_document(kept), max_lines=max_lines, max_bytes=max_bytes):
        category = _largest_category(kept)
        victim = min((entry for entry in kept if entry.category == category), key=_eviction_order)
        kept.remove(victim)
        evicted.append(victim)
    return kept, evicted


@dataclass(frozen=True)
class RoundOutcome:
    """What one consolidation round did, for logging and operator feedback."""

    applied: int
    rejected: int
    consolidated: int
    discarded: int
    still_pending: int


def _validate_operation(
    operation: MemoryOperation,
    observations: list[str],
    entries: list[str],
    *,
    entries_by_id: dict[str, MemoryEntry],
    round_observation_ids: set[str],
    claimed: set[str],
    claimed_entries: set[str],
) -> str | None:
    """Return why this operation cannot be applied, or ``None`` when it can.

    ``observations`` and ``entries`` are the operation's ids already deduplicated by the caller,
    so validation and apply reason about exactly the same targets. Semantic checks the flat schema
    deliberately does not make: reference validity, per-op shape, and the same-category fence on MERGE.
    """
    if not observations:
        return "references no observation"
    if unknown := [oid for oid in observations if oid not in round_observation_ids]:
        return f"references observations outside this round: {unknown}"
    if taken := [oid for oid in observations if oid in claimed]:
        return f"references observations already claimed by an earlier operation: {taken}"

    if unknown := [eid for eid in entries if eid not in entries_by_id]:
        return f"references unknown or superseded entries: {unknown}"
    # ``entries_by_id`` is the pre-round snapshot, so a second operation would reason about
    # content the first already replaced (and for UPDATE/MERGE would orphan a successor link).
    if retargeted := [eid for eid in entries if eid in claimed_entries]:
        return f"targets entries an earlier operation already changed: {retargeted}"

    content = (operation.content or "").strip()
    match operation.op:
        case "ADD":
            if entries:
                return "ADD must not target existing entries"
            if not content:
                return "ADD without content"
            # Pydantic's literal already rejected any non-empty value outside the choices.
            if operation.category is None:
                return "ADD without a category"
        case "UPDATE":
            if len(entries) != 1:
                return f"UPDATE must target exactly one entry, got {len(entries)}"
            if not content:
                return "UPDATE without content"
        case "MERGE":
            if len(entries) < 2:
                return f"MERGE must target at least two entries, got {len(entries)}"
            if not content:
                return "MERGE without content"
            if len(categories := {entries_by_id[eid].category for eid in entries}) > 1:
                return f"MERGE crosses categories: {sorted(categories)}"
        case "CONFIRM":
            if len(entries) != 1:
                return f"CONFIRM must target exactly one entry, got {len(entries)}"
        case "DISCARD":
            if entries:
                return "DISCARD must not target existing entries"
            if not (operation.reason or "").strip():
                return "DISCARD without a reason"
        case unhandled:
            # Unreachable while every literal has an arm; keeps a newly added op from being
            # waved through unvalidated and then silently no-oping in the apply match.
            return f"unhandled operation {unhandled}"
    return None


def _source_run_id(observations: list[MemoryObservation]) -> str | None:
    """The run behind the newest source observation — the closest thing to "who taught us this".

    Sorted here rather than relied on: ``observations`` arrives in the order the model listed the
    ids, which carries no chronology.
    """
    for observation in sorted(observations, key=lambda obs: obs.created_at, reverse=True):
        if observation.run_id:
            return observation.run_id
    return None


def _accept_operations(
    repo_id: str,
    operations: Sequence[MemoryOperation],
    entries_by_id: dict[str, MemoryEntry],
    observations_by_id: dict[str, MemoryObservation],
) -> tuple[list[AcceptedOperation], set[str], int]:
    """Partition a round's operations into the applicable ones and a rejected count.

    Ids are deduplicated once, here, and carried into the apply phase so both reason about the
    same targets. Returns the accepted operations, the observation ids they claim, and how many
    operations were rejected.
    """
    claimed: set[str] = set()
    claimed_entries: set[str] = set()
    accepted: list[AcceptedOperation] = []
    rejected = 0

    for operation in operations:
        observation_ids = list(dict.fromkeys(operation.observation_ids))
        entry_ids = list(dict.fromkeys(operation.entry_ids))
        if reason := _validate_operation(
            operation,
            observation_ids,
            entry_ids,
            entries_by_id=entries_by_id,
            round_observation_ids=set(observations_by_id),
            claimed=claimed,
            claimed_entries=claimed_entries,
        ):
            logger.warning(
                "consolidation: rejected %s operation for repo %s — %s; its observations keep their current "
                "status and are re-queued for a later round. Operation: %s",
                operation.op,
                repo_id,
                reason,
                operation.model_dump(exclude_none=True),
            )
            rejected += 1
            continue
        claimed.update(observation_ids)
        claimed_entries.update(entry_ids)
        accepted.append((operation, entry_ids, [observations_by_id[oid] for oid in observation_ids]))

    return accepted, claimed, rejected


def _write_operations(
    repo_id: str, accepted: Sequence[AcceptedOperation], entries_by_id: dict[str, MemoryEntry], now: datetime
) -> tuple[list, list, list[MemoryEntry]]:
    """Perform each accepted operation's entry writes.

    Returns the observation ids to mark consolidated, the ones to mark discarded, and the entries
    created this round. Must run inside the round's transaction.
    """
    consolidated_ids: list = []
    discarded_ids: list = []
    created: list[MemoryEntry] = []

    for operation, entry_ids, sources in accepted:
        match operation.op:
            case "ADD":
                created.append(_create_entry(repo_id, cast("str", operation.category), operation, sources, now))
                consolidated_ids += [source.pk for source in sources]
            case "UPDATE" | "MERGE":
                superseded = [entries_by_id[eid] for eid in entry_ids]
                entry = _create_entry(repo_id, superseded[0].category, operation, sources, now)
                for previous in superseded:
                    previous.supersede(entry)
                created.append(entry)
                consolidated_ids += [source.pk for source in sources]
            case "CONFIRM":
                entry = entries_by_id[entry_ids[0]]
                entry.confirm(now)
                entry.observations.add(*sources)
                consolidated_ids += [source.pk for source in sources]
            case "DISCARD":
                # The only operation that destroys a learning, and nothing persists the
                # justification — the log is the whole audit trail.
                logger.info(
                    "consolidation: repo %s discarding %d observation(s) — %s: %s",
                    repo_id,
                    len(sources),
                    operation.reason,
                    [str(source.pk) for source in sources],
                )
                discarded_ids += [source.pk for source in sources]
            case unhandled:
                raise ValueError(f"unhandled memory operation {unhandled}")

    return consolidated_ids, discarded_ids, created


def _apply_round(
    repo_id: str,
    operations: Sequence[MemoryOperation],
    observations: Sequence[MemoryObservation],
    entries: Sequence[MemoryEntry],
    *,
    max_lines: int,
    max_bytes: int,
) -> RoundOutcome | None:
    """Validate then apply a round's operations, re-rendering the document.

    Everything — entry writes, observation status flips and the render — commits in a single
    transaction, so a failure leaves the round's observations in whatever status they arrived
    with and the stored document untouched. Returns ``None`` when no operation survived
    validation (nothing is written).
    """
    entries_by_id = {str(entry.pk): entry for entry in entries}
    observations_by_id = {str(observation.pk): observation for observation in observations}
    accepted, claimed, rejected = _accept_operations(repo_id, operations, entries_by_id, observations_by_id)

    if not accepted:
        logger.error(
            "consolidation: no valid operation for repo %s (%d rejected); leaving %d observations pending",
            repo_id,
            rejected,
            len(observations),
        )
        return None
    if rejected * 2 >= len(operations):
        # One survivor out of fifty would otherwise report as a healthy round: the per-rejection
        # lines are warnings, which never become Sentry events.
        logger.error(
            "consolidation: repo %s rejected %d of %d operations (only %d applied) — the consolidation "
            "model's output is degraded",
            repo_id,
            rejected,
            len(operations),
            len(accepted),
        )

    now = timezone.now()

    with transaction.atomic():
        consolidated_ids, discarded_ids, created = _write_operations(repo_id, accepted, entries_by_id, now)

        if consolidated_ids:
            MemoryObservation.objects.filter(pk__in=consolidated_ids).update(status=ObservationStatus.CONSOLIDATED)
        if discarded_ids:
            MemoryObservation.objects.filter(pk__in=discarded_ids).update(status=ObservationStatus.DISCARDED)

        surviving = [entry for entry in entries if entry.status == EntryStatus.ACTIVE] + created
        kept, evicted = prune_to_budget(surviving, max_lines=max_lines, max_bytes=max_bytes)
        for entry in evicted:
            entry.supersede()

        memory, _created = RepositoryMemory.objects.get_or_create(repo_id=repo_id)
        memory.content = render_memory_document(kept)
        memory.last_consolidated_at = now
        memory.save(update_fields=["content", "last_consolidated_at", "updated_at"])

    if evicted:
        # Irreversible in practice — nothing in the product surfaces a superseded entry — so this
        # is an error, not a warning: it must reach Sentry with enough detail to reconstruct.
        logger.error(
            "consolidation: evicted %d of %d entr(ies) for repo %s to fit the %d line / %d byte render budget: %s",
            len(evicted),
            len(surviving),
            repo_id,
            max_lines,
            max_bytes,
            [(str(entry.pk), entry.category) for entry in evicted],
        )
    return RoundOutcome(
        applied=len(accepted),
        rejected=rejected,
        consolidated=len(consolidated_ids),
        discarded=len(discarded_ids),
        still_pending=len(observations) - len(claimed),
    )


def _create_entry(
    repo_id: str, category: str, operation: MemoryOperation, sources: list[MemoryObservation], now: datetime
) -> MemoryEntry:
    entry = MemoryEntry.objects.create(
        repo_id=repo_id,
        category=category,
        content=cast("str", operation.content).strip(),
        source_run_id=_source_run_id(sources),
        created_at=now,
        last_confirmed_at=now,
    )
    # ``add`` rather than ``set``: the entry was created a statement ago, so there is nothing to
    # diff against and ``set``'s read-back of current relations is a guaranteed-empty query.
    entry.observations.add(*sources)
    return entry


async def run_consolidation_round(
    repo_id: str, config, observations: Sequence[MemoryObservation]
) -> RoundOutcome | None:
    """Decide and apply one round of operations for ``observations``.

    Shared by the scheduled task and the backfill command: the caller owns which observations the
    round sees and whether the repository is in a fit state to consolidate, this owns the LLM call
    and the apply. Returns ``None`` when nothing was applied.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from memory.prompts import consolidation_human, consolidation_system

    # Empty override → reuse the repo's agent model.
    consolidation_model = site_settings.memory_consolidation_model_name or config.models.agent.model
    try:
        structured_llm = _build_structured_llm(
            MemoryOperations, (consolidation_model, config.models.agent.fallback_model)
        )
    except RuntimeError, ValueError:
        # RuntimeError: provider disabled / no API key / unknown provider_type.
        # ValueError: empty or unparseable model spec / no matching provider row.
        # Both are precondition failures, not crashes — skip with an error, like every other.
        logger.exception("consolidation: model unavailable/misconfigured for repo %s, skipping", repo_id)
        return None

    entries = [entry async for entry in MemoryEntry.objects.filter(repo_id=repo_id).active().order_by("created_at")]
    entries_text = "\n".join(
        f"- {entry.pk} | {entry.category} | {entry.last_confirmed_at:%Y-%m-%d} | {entry.content}" for entry in entries
    )
    observations_text = "\n".join(
        f"- {observation.pk} | {observation.category} | {observation.created_at:%Y-%m-%d} | {observation.content}"
        for observation in observations
    )

    result = cast(
        "MemoryOperations",
        await structured_llm.with_config(
            run_name="MemoryConsolidation", tags=["MemoryConsolidation"], metadata={"repo_id": repo_id}
        ).ainvoke([
            SystemMessage(content=cast("str", consolidation_system.format().content)),
            HumanMessage(
                content=cast(
                    "str",
                    consolidation_human.format(
                        repo_id=repo_id, entries=entries_text, observations=observations_text
                    ).content,
                )
            ),
        ]),
    )

    if result is None:
        logger.error(
            "consolidation: structured output returned nothing for repo %s (model %s) — schema binding or "
            "parse failure; leaving %d observation(s) pending",
            repo_id,
            consolidation_model,
            len(observations),
        )
        return None
    if not result.operations:
        logger.error(
            "consolidation: model returned an empty operation list for repo %s; leaving %d observation(s) pending",
            repo_id,
            len(observations),
        )
        return None

    return await sync_to_async(_apply_round)(
        repo_id,
        result.operations,
        observations,
        entries,
        max_lines=site_settings.memory_max_lines,
        max_bytes=site_settings.memory_max_bytes,
    )


async def _document_would_be_discarded(repo_id: str) -> bool:
    """Whether consolidating now would re-render a stored document away.

    True for a repository whose document predates entries: the round would rebuild it from an
    empty entry set. ``backfill_memory_entries`` is the repair, and is exempt because populating
    those entries is exactly what it does.
    """
    return (
        not await MemoryEntry.objects.filter(repo_id=repo_id).active().aexists()
        and await RepositoryMemory.objects.filter(repo_id=repo_id).exclude(content="").aexists()
    )


@task()
async def consolidate_memory_task(repo_id: str) -> None:
    """Fold pending observations into the repository's memory entries ("dreaming").

    The model decides per observation — add a fact, correct or merge existing entries, confirm a
    duplicate, discard noise — and only the entries an operation names are rewritten. The document
    is then re-rendered from the surviving entries; budget pressure can additionally retire
    entries no operation mentioned. Failures propagate to django-tasks (logged + marked failed);
    agent runs are never affected — this runs out-of-band.

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
        obs async for obs in MemoryObservation.objects.filter(repo_id=repo_id).pending().order_by("created_at")
    ]
    if not observations:
        logger.info("consolidate_memory_task: no pending observations for repo %s, skipping", repo_id)
        return

    if await _document_would_be_discarded(repo_id):
        logger.error(
            "consolidate_memory_task: repo %s has a memory document but no entries, so re-rendering would "
            "discard it; run `backfill_memory_entries --repo-id %s` first. Leaving %d observation(s) pending.",
            repo_id,
            repo_id,
            len(observations),
        )
        outcome = None
    else:
        outcome = await run_consolidation_round(repo_id, config, observations)

    # Record the attempt whatever it produced, so the cron's cooldown applies to a repository
    # whose consolidation keeps failing too — otherwise it pays for a full round every hour.
    await RepositoryMemory.objects.aupdate_or_create(repo_id=repo_id, defaults={"last_attempted_at": timezone.now()})

    if outcome is None:
        return
    if outcome.still_pending:
        logger.error(
            "consolidate_memory_task: repo %s — the model left %d of %d observation(s) unclaimed; they stay "
            "pending and will be retried, which never converges if the model keeps skipping them",
            repo_id,
            outcome.still_pending,
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
    from langchain_core.messages import HumanMessage, SystemMessage
    from sessions.models import Run

    from core.checkpointer import aresolve_thread_messages, open_checkpointer
    from memory.prompts import extraction_human, extraction_system
    from memory.transcript import serialize_transcript

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

    thread_config = {"configurable": {"thread_id": str(run.session_id)}}
    async with open_checkpointer() as checkpointer:
        checkpoint_tuple = await checkpointer.aget_tuple(thread_config)
        if checkpoint_tuple is None:
            # Benign: the checkpoint expired from Redis before this task ran.
            logger.info(
                "extract_observations_task: checkpoint missing/expired for thread %s (run=%s), skipping",
                run.session_id,
                run_id,
            )
            return
        channel_values = (checkpoint_tuple.checkpoint or {}).get("channel_values", {})
        # ``messages`` is stored in a deepagents ``DeltaChannel`` and is usually absent from
        # ``channel_values`` — reconstruct it from the delta write history.
        messages = await aresolve_thread_messages(checkpointer, thread_config, channel_values)

    if not messages:
        # A present checkpoint with no messages even after DeltaChannel reconstruction signals
        # a real defect (serialization or channel-name drift), not normal TTL expiry — louder.
        logger.warning(
            "extract_observations_task: checkpoint present but has no messages for thread %s (run=%s); "
            "available channels: %s — skipping (serialization or channel-name drift?)",
            run.session_id,
            run_id,
            sorted(channel_values),
        )
        return

    if not any(getattr(message, "type", None) == "ai" for message in messages):
        # No agent turns means no agent behaviour to learn from (e.g. the sandbox never came up),
        # so skip the model call rather than pay for an almost certainly empty extraction.
        logger.info(
            "extract_observations_task: run %s has no AI turns (%d message(s)), nothing to extract, skipping",
            run_id,
            len(messages),
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
