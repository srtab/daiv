from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from core.site_settings import site_settings
from memory.llm import build_structured_llm
from memory.models import EntryStatus, MemoryEntry, MemoryObservation, ObservationStatus, RepositoryMemory
from memory.render import prune_to_budget, render_memory_document
from memory.schemas import MAX_OPERATIONS, MemoryOperations

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from memory.schemas import MemoryOperation

logger = logging.getLogger("daiv.memory")

# ``content`` has no parse-time bound (see ``memory.schemas``), so a runaway generation reaches the
# rejection log intact — clamp it there rather than let one operation write a megabyte log line.
LOG_EXCERPT_CHARS = 500


def _loggable(operation: MemoryOperation) -> dict:
    dumped = operation.model_dump(exclude_none=True)
    if content := dumped.get("content"):
        dumped["content"] = content[:LOG_EXCERPT_CHARS]
    return dumped


@dataclass(frozen=True)
class RoundOutcome:
    """What one consolidation round did, for logging and operator feedback."""

    applied: int
    rejected: int
    consolidated: int
    discarded: int
    still_pending: int
    # The part of ``still_pending`` we deferred ourselves at ``MAX_OPERATIONS``, as opposed to
    # observations the model simply never named. Only the latter is a model fault.
    truncated: int = 0


class ConsolidationRound:
    """One round of memory operations for a repository.

    Owns the pre-round snapshot operations are validated against, and the claim bookkeeping
    that stops two operations from touching the same observation or entry.
    """

    def __init__(
        self,
        repo_id: str,
        operations: Sequence[MemoryOperation],
        observations: Sequence[MemoryObservation],
        entries: Sequence[MemoryEntry],
        deferred: Sequence[MemoryOperation] = (),
    ) -> None:
        self.repo_id = repo_id
        self.operations = operations
        # Sliced off by the caller before the round so the degraded-round ratio and ``still_pending``
        # measure the operations we actually tried; kept here only to attribute the deferred tail.
        self.deferred = deferred
        self.observations = observations
        self.entries = entries
        self.entries_by_id = {str(entry.pk): entry for entry in entries}
        self.observations_by_id = {str(observation.pk): observation for observation in observations}
        self.claimed: set[str] = set()
        self.claimed_entries: set[str] = set()

    def validate(self, operation: MemoryOperation) -> str | None:
        """Return why this operation cannot be applied, or ``None`` when it can.

        Self-consistency is ``MemoryOperation.shape_error``; this adds the checks that need the
        round's snapshot: reference validity and the same-category fence on MERGE.
        """
        if reason := operation.shape_error():
            return reason
        if unknown := [oid for oid in operation.observation_ids if oid not in self.observations_by_id]:
            return f"references observations outside this round: {unknown}"
        if taken := [oid for oid in operation.observation_ids if oid in self.claimed]:
            return f"references observations already claimed by an earlier operation: {taken}"
        if unknown := [eid for eid in operation.entry_ids if eid not in self.entries_by_id]:
            return f"references unknown or superseded entries: {unknown}"
        # ``entries_by_id`` is the pre-round snapshot, so a second operation would reason about
        # content the first already replaced (and for UPDATE/MERGE would orphan a successor link).
        if retargeted := [eid for eid in operation.entry_ids if eid in self.claimed_entries]:
            return f"targets entries an earlier operation already changed: {retargeted}"
        if operation.op == "MERGE":
            categories = {self.entries_by_id[eid].category for eid in operation.entry_ids}
            if len(categories) > 1:
                return f"MERGE crosses categories: {sorted(categories)}"
        return None

    def accept(self) -> tuple[list[MemoryOperation], int]:
        """Split the round's operations into the applicable ones and a rejected count.

        ``apply()`` calls this itself; calling both re-logs the rejections.
        """
        # Reset so a second call re-decides from the snapshot instead of finding everything claimed.
        self.claimed = set()
        self.claimed_entries = set()
        accepted: list[MemoryOperation] = []
        rejected = 0
        for operation in self.operations:
            if reason := self.validate(operation):
                logger.warning(
                    "consolidation: rejected %s operation for repo %s — %s; its observations keep their current "
                    "status and are re-queued for a later round. Operation: %s",
                    operation.op,
                    self.repo_id,
                    reason,
                    _loggable(operation),
                )
                rejected += 1
                continue
            self.claimed.update(operation.observation_ids)
            self.claimed_entries.update(operation.entry_ids)
            accepted.append(operation)
        return accepted, rejected

    def apply(self, *, max_lines: int, max_bytes: int) -> RoundOutcome | None:
        """Validate then apply the round's operations, re-rendering the document.

        Everything — entry writes, observation status flips and the render — commits in a single
        transaction, so a failure leaves the round's observations in whatever status they arrived
        with and the stored document untouched. Returns ``None`` when no operation survived
        validation (nothing is written).
        """
        accepted, rejected = self.accept()

        if not accepted:
            logger.error(
                "consolidation: no valid operation for repo %s (%d rejected); leaving %d observations pending",
                self.repo_id,
                rejected,
                len(self.observations),
            )
            return None
        if rejected * 2 >= len(self.operations):
            # One survivor out of fifty would otherwise report as a healthy round: the per-rejection
            # lines are warnings, which never become Sentry events.
            logger.error(
                "consolidation: repo %s rejected %d of %d operations (only %d applied) — the consolidation "
                "model's output is degraded",
                self.repo_id,
                rejected,
                len(self.operations),
                len(accepted),
            )

        now = timezone.now()

        with transaction.atomic():
            consolidated_ids, discarded_ids, created = self._write(accepted, now)

            if consolidated_ids:
                MemoryObservation.objects.filter(pk__in=consolidated_ids).update(status=ObservationStatus.CONSOLIDATED)
            if discarded_ids:
                MemoryObservation.objects.filter(pk__in=discarded_ids).update(status=ObservationStatus.DISCARDED)

            surviving = [entry for entry in self.entries if entry.status == EntryStatus.ACTIVE] + created
            kept, evicted = prune_to_budget(surviving, max_lines=max_lines, max_bytes=max_bytes)
            for entry in evicted:
                entry.supersede()

            memory, _created = RepositoryMemory.objects.get_or_create(repo_id=self.repo_id)
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
                self.repo_id,
                max_lines,
                max_bytes,
                [(str(entry.pk), entry.category) for entry in evicted],
            )
        # Intersected with what is actually still pending, so ``truncated`` stays a subset of
        # ``still_pending``: the task subtracts one from the other to find what the model skipped.
        still_pending_ids = self.observations_by_id.keys() - self.claimed
        deferred_ids = {oid for operation in self.deferred for oid in operation.observation_ids}
        return RoundOutcome(
            applied=len(accepted),
            rejected=rejected,
            consolidated=len(consolidated_ids),
            discarded=len(discarded_ids),
            still_pending=len(still_pending_ids),
            truncated=len(deferred_ids & still_pending_ids),
        )

    def _write(self, accepted: Sequence[MemoryOperation], now: datetime) -> tuple[list, list, list[MemoryEntry]]:
        """Perform each accepted operation's entry writes.

        Returns the observation ids to mark consolidated, the ones to mark discarded, and the
        entries created this round. Must run inside the round's transaction.
        """
        consolidated_ids: list = []
        discarded_ids: list = []
        created: list[MemoryEntry] = []

        for operation in accepted:
            sources = [self.observations_by_id[oid] for oid in operation.observation_ids]
            match operation.op:
                case "ADD":
                    created.append(self._create_entry(cast("str", operation.category), operation, sources, now))
                    consolidated_ids += [source.pk for source in sources]
                case "UPDATE" | "MERGE":
                    superseded = [self.entries_by_id[eid] for eid in operation.entry_ids]
                    entry = self._create_entry(superseded[0].category, operation, sources, now)
                    for previous in superseded:
                        previous.supersede(entry)
                    created.append(entry)
                    consolidated_ids += [source.pk for source in sources]
                case "CONFIRM":
                    entry = self.entries_by_id[operation.entry_ids[0]]
                    entry.confirm(now)
                    entry.observations.add(*sources)
                    consolidated_ids += [source.pk for source in sources]
                case "DISCARD":
                    # The only operation that destroys a learning, and nothing persists the
                    # justification — the log is the whole audit trail.
                    logger.info(
                        "consolidation: repo %s discarding %d observation(s) — %s: %s",
                        self.repo_id,
                        len(sources),
                        operation.reason,
                        [str(source.pk) for source in sources],
                    )
                    discarded_ids += [source.pk for source in sources]
                case unhandled:
                    raise ValueError(f"unhandled memory operation {unhandled}")

        return consolidated_ids, discarded_ids, created

    def _create_entry(
        self, category: str, operation: MemoryOperation, sources: list[MemoryObservation], now: datetime
    ) -> MemoryEntry:
        entry = MemoryEntry.objects.create(
            repo_id=self.repo_id,
            category=category,
            content=cast("str", operation.content),
            source_run_id=_source_run_id(sources),
            created_at=now,
            last_confirmed_at=now,
        )
        # ``add`` rather than ``set``: the entry was created a statement ago, so there is nothing to
        # diff against and ``set``'s read-back of current relations is a guaranteed-empty query.
        entry.observations.add(*sources)
        return entry


def _source_run_id(observations: list[MemoryObservation]) -> str | None:
    """The run behind the newest source observation — the closest thing to "who taught us this".

    Sorted here rather than relied on: ``observations`` arrives in the order the model listed the
    ids, which carries no chronology.
    """
    for observation in sorted(observations, key=lambda obs: obs.created_at, reverse=True):
        if observation.run_id:
            return observation.run_id
    return None


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
        structured_llm = build_structured_llm(
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

    operations, deferred = result.operations[:MAX_OPERATIONS], result.operations[MAX_OPERATIONS:]
    if deferred:
        logger.warning(
            "consolidation: repo %s — model returned %d operations, over the %d cap; the rest are deferred",
            repo_id,
            len(result.operations),
            MAX_OPERATIONS,
        )

    round_ = ConsolidationRound(repo_id, operations, observations, entries, deferred=deferred)
    return await sync_to_async(round_.apply)(
        max_lines=site_settings.memory_max_lines, max_bytes=site_settings.memory_max_bytes
    )


def document_would_be_discarded(repo_id: str) -> bool:
    """Whether consolidating now would re-render a stored document away.

    True for a repository whose document predates entries: the round would rebuild it from an
    empty entry set. ``backfill_memory_entries`` is the repair, and is exempt because populating
    those entries is exactly what it does.

    Sync so the dashboard's consolidate view and the task share one definition of the guard.
    """
    return (
        not MemoryEntry.objects.filter(repo_id=repo_id).active().exists()
        and RepositoryMemory.objects.filter(repo_id=repo_id).with_document().exists()
    )
