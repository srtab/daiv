from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from memory.models import MemoryObservation, ObservationStatus
from memory.tasks import CONSOLIDATION_MIN_PENDING, consolidate_memory_task

logger = logging.getLogger("daiv.memory")


class Command(BaseCommand):
    help = "Consolidate pending memory observations into the repository memory document."

    def add_arguments(self, parser):
        parser.add_argument("--repo-id", required=True, help="Repository ID to consolidate (e.g. group/project).")
        parser.add_argument(
            "--force", action="store_true", help="Consolidate even below the pending-observation threshold."
        )

    def handle(self, *args, **options):
        repo_id = options["repo_id"]
        pending = MemoryObservation.objects.filter(repo_id=repo_id, status=ObservationStatus.PENDING).count()
        if pending == 0:
            logger.warning("No pending observations for %s; nothing to do.", repo_id)
            return
        if pending < CONSOLIDATION_MIN_PENDING and not options["force"]:
            raise CommandError(
                f"Only {pending} pending observations (threshold is {CONSOLIDATION_MIN_PENDING}). "
                "Use --force to consolidate anyway."
            )
        # Run the task in-process for immediate operator feedback instead of enqueueing to the worker.
        consolidate_memory_task.call(repo_id)
        logger.info("Consolidation completed for %s (%d pending observations).", repo_id, pending)
