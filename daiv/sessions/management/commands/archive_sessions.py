from __future__ import annotations

from django.core.management.base import BaseCommand

from asgiref.sync import async_to_sync

from sessions.archive import aarchive_thread
from sessions.models import Session


class Command(BaseCommand):
    help = (
        "Copy the live checkpoint of every session without an archive into it. "
        "Run once after deploying the session archive, before those checkpoints expire."
    )

    def handle(self, *args, **options):
        archived, total = async_to_sync(self._aarchive_all)()
        self.stdout.write(
            f"Archived {archived} of {total} session(s) without an archive; "
            "the rest had no checkpoint left (failures are logged)."
        )

    async def _aarchive_all(self) -> tuple[int, int]:
        from core.checkpointer import open_checkpointer

        unarchived = Session.objects.filter(archives__isnull=True).values_list("thread_id", flat=True)
        thread_ids = [thread_id async for thread_id in unarchived]
        archived = 0
        async with open_checkpointer() as checkpointer:
            for thread_id in thread_ids:
                archived += await aarchive_thread(checkpointer, thread_id)
        return archived, len(thread_ids)
