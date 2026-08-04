from __future__ import annotations

import logging
from itertools import batched

from django.core.management.base import BaseCommand, CommandError

from asgiref.sync import async_to_sync

from codebase.repo_config import RepositoryConfig
from memory.consolidation import run_consolidation_round
from memory.models import MemoryEntry, MemoryObservation
from memory.render import document_size, render_memory_document

logger = logging.getLogger("daiv.memory")


class Command(BaseCommand):
    help = (
        "Rebuild a repository's memory entries by replaying its historical consolidated observations "
        "through the consolidation operations pass, oldest first. Makes LLM calls. Safe to re-run: "
        "only observations not yet linked to an entry are replayed. Ignores the memory enabled flags — "
        "this is an operator repair action."
    )

    def add_arguments(self, parser):
        parser.add_argument("--repo-id", required=True, help="Repository ID to backfill (e.g. group/project).")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=20,
            choices=range(1, 51),
            metavar="[1-50]",
            help="Observations per operations round (default: 20).",
        )

    def handle(self, *args, **options):
        repo_id = options["repo_id"]
        unreplayed = MemoryObservation.objects.filter(repo_id=repo_id).unreplayed()
        observations = list(unreplayed.order_by("created_at"))
        if not observations:
            logger.warning("No unreplayed consolidated observations for %s; nothing to do.", repo_id)
            return

        config = RepositoryConfig.get_config(repo_id)
        batch_size = options["batch_size"]
        # strict=False: the final batch is short whenever the count is not a multiple of the size.
        batches = list(batched(observations, batch_size, strict=False))
        logger.info(
            "Backfilling %s: %d observation(s) in %d batch(es) of %d.",
            repo_id,
            len(observations),
            len(batches),
            batch_size,
        )

        barren = 0
        for number, batch in enumerate(batches, start=1):
            outcome = async_to_sync(run_consolidation_round)(repo_id, config, batch)
            if outcome is None:
                logger.warning("Batch %d/%d applied nothing; its observations remain unreplayed.", number, len(batches))
                barren += 1
                continue
            logger.info(
                "Batch %d/%d: %d operation(s) applied, %d rejected, %d discarded.",
                number,
                len(batches),
                outcome.applied,
                outcome.rejected,
                outcome.discarded,
            )

        entries = list(MemoryEntry.objects.filter(repo_id=repo_id).active())
        remaining = unreplayed.count()
        lines, size = document_size(render_memory_document(entries))
        logger.info(
            "Backfill finished for %s: %d active entr(ies), document is %d line(s) / %d byte(s), "
            "%d observation(s) still unreplayed (re-run to retry).",
            repo_id,
            len(entries),
            lines,
            size,
            remaining,
        )
        if barren:
            # Raised after the summary so the operator keeps the diagnostic: this is a repair
            # action, and a caller chaining off it must be able to tell it accomplished nothing.
            raise CommandError(
                f"{barren} of {len(batches)} batch(es) applied nothing and {remaining} observation(s) are still "
                "unreplayed; see the log above for the reason, then re-run."
            )
