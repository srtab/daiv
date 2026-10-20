from django.tasks.backends.immediate import ImmediateBackend as DJImmediateBackend

from core.backends.deduplicating import DeduplicatingTask


class ImmediateBackend(DJImmediateBackend):
    """
    Immediate backend that adds support for deduplicating tasks types that inherit from `DeduplicatingTask`.
    """

    task_class = DeduplicatingTask
