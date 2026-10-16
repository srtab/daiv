from __future__ import annotations

import logging

from django.core.management.base import BaseCommand, CommandError

from asgiref.sync import async_to_sync

from sessions.archive import aarchive_thread
from sessions.models import Session

logger = logging.getLogger("daiv.sessions")


class Command(BaseCommand):
    help = (
        "Copy the live checkpoint of every session without an archive into it. Run once after migrating "
        "(sessions 0014), before those checkpoints expire; running it again retries the sessions that failed."
    )

    def handle(self, *args, **options):
        archived, empty, failed = async_to_sync(self._aarchive_all)()
        self.stdout.write(f"Archived {archived} session(s); {empty} had nothing left to archive.")
        if failed:
            raise CommandError(
                f"Failed to archive {len(failed)} session(s), see the log; run the command again to retry: "
                + ", ".join(failed)
            )

    async def _aarchive_all(self) -> tuple[int, int, list[str]]:
        from core.checkpointer import open_checkpointer

        unarchived = Session.objects.filter(archives__isnull=True).values_list("thread_id", flat=True)
        thread_ids = [thread_id async for thread_id in unarchived]
        archived, empty, failed = 0, 0, []
        async with open_checkpointer() as checkpointer:
            for thread_id in thread_ids:
                try:
                    written = await aarchive_thread(checkpointer, thread_id)
                except Exception:
                    logger.exception("Failed to archive thread %s", thread_id)
                    failed.append(thread_id)
                    continue
                if written:
                    archived += 1
                else:
                    empty += 1
        return archived, empty, failed
