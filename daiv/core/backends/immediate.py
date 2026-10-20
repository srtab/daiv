from django.tasks.backends.immediate import ImmediateBackend as DJImmediateBackend

from core.backends.deduplicating import DeduplicatingTask


class ImmediateBackend(DJImmediateBackend):
    """
    Immediate backend that adds support for deduplicating tasks types that inherit from `DeduplicatingTask`.
    """

    task_class = DeduplicatingTask

    async def aget_or_enqueue(self, task, args, kwargs):
        """Run the task now, like every enqueue here: nothing is kept to deduplicate against, so it always creates."""
        return await self.aenqueue(task, args, kwargs), True
