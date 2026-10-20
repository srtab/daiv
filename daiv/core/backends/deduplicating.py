from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from django.db import connection, transaction
from django.db.models import Q
from django.utils.version import PY311

from asgiref.sync import sync_to_async
from django_tasks.base import Task, TaskResult, TaskResultStatus
from django_tasks.utils import normalize_json
from django_tasks_db import DatabaseBackend
from django_tasks_db.models import DBTaskResult

if TYPE_CHECKING:
    from django_tasks_db.backend import TaskResult as DatabaseTaskResult

logger = logging.getLogger("daiv.tasks")

T = TypeVar("T")
P = ParamSpec("P")


@dataclass(frozen=True, slots=PY311, kw_only=True)
class DeduplicatingTask(Task[P, T]):
    """
    Task that skips enqueuing duplicate tasks based on module path, args, and kwargs.
    """

    dedup: bool = False
    """
    Whether to deduplicate tasks based on module path, args, and kwargs. Defaults to False.
    """

    dedup_on: tuple[str, ...] | None = None
    """
    The keyword arguments that identify a duplicate, when the others may differ between repeats. Defaults to None:
    every argument. Those arguments must then be passed by keyword.
    """

    dedup_successful: bool = True
    """
    Whether a successful task still counts as a duplicate. False: only a queued or running one does.
    """

    async def aget_or_enqueue(self, *args: P.args, **kwargs: P.kwargs) -> tuple[TaskResult[T], bool]:
        """``aenqueue``, also saying whether it created the task: ``False`` when ``dedup`` matched an earlier one."""
        return await self.get_backend().aget_or_enqueue(self, args, kwargs)  # ty: ignore[unresolved-attribute]

    async def afind_duplicate(self, *args: P.args, **kwargs: P.kwargs) -> TaskResult[T] | None:
        """The earlier task :meth:`aget_or_enqueue` would hand back for these arguments, read without its lock."""
        return await self.get_backend().afind_duplicate(self, args, kwargs)  # ty: ignore[unresolved-attribute]


class DeduplicatingDatabaseBackend(DatabaseBackend):
    """
    Database backend that skips enqueuing duplicate tasks based on module path, args, and kwargs.
    """

    task_class = DeduplicatingTask

    def enqueue(self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs) -> DatabaseTaskResult[T]:
        """
        Enqueue a task unless a matching dedup key is already queued, running or done.

        Returns the existing TaskResult when a duplicate is detected.
        """
        return self.get_or_enqueue(task, args, kwargs)[0]

    async def aenqueue(self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs) -> DatabaseTaskResult[T]:
        """Async twin of :meth:`enqueue`."""
        return (await self.aget_or_enqueue(task, args, kwargs))[0]

    def get_or_enqueue(
        self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs
    ) -> tuple[DatabaseTaskResult[T], bool]:
        """
        Enqueue a task unless a matching dedup key is already queued, running or done.

        Args:
            task: Task instance being enqueued.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            The task result for the new or existing task, and whether this call created it. The lookup and the insert
            share one advisory lock, so of concurrent calls with one key exactly one gets ``True``.
        """
        if not task.dedup:
            return super().enqueue(task, args, kwargs), True

        dedup_key = self._dedup_key(task, args, kwargs)
        with transaction.atomic():
            self._lock_dedup_key(task.module_path, dedup_key)
            if existing_result := self._get_existing_task_result(task, dedup_key):
                logger.info("Skipping duplicate task: %s with args: %r and kwargs: %r", task.module_path, args, kwargs)
                return existing_result.task_result, False
            return super().enqueue(task, args, kwargs), True

    async def aget_or_enqueue(
        self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs
    ) -> tuple[DatabaseTaskResult[T], bool]:
        """
        Async twin of :meth:`get_or_enqueue`: the parent's ``aenqueue`` writes the row itself rather than calling
        ``enqueue``, so a ``dedup`` task goes through :meth:`get_or_enqueue` instead.
        """
        if not task.dedup:
            return await super().aenqueue(task, args, kwargs), True

        return await sync_to_async(self.get_or_enqueue)(task, args, kwargs)

    async def afind_duplicate(
        self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs
    ) -> DatabaseTaskResult[T] | None:
        """
        The task :meth:`aget_or_enqueue` would hand back, looked up without the advisory lock: a cheap early look for
        a caller with work to skip on a repeat. A duplicate enqueued concurrently can be missed; the enqueue still
        catches it.
        """
        if not task.dedup:
            return None
        existing = await sync_to_async(self._get_existing_task_result)(task, self._dedup_key(task, args, kwargs))
        return existing.task_result if existing else None

    def _dedup_key(self, task: DeduplicatingTask[P, T], args: P.args, kwargs: P.kwargs) -> dict[str, Any]:
        """The JSON a duplicate matches: the whole ``args_kwargs``, or only the ``dedup_on`` kwargs."""
        if task.dedup_on is None:
            return normalize_json({"args": args, "kwargs": kwargs})
        if args:
            raise TypeError(f"{task.module_path} dedups on {task.dedup_on}: pass its arguments by keyword")
        return normalize_json({name: kwargs.get(name) for name in task.dedup_on})

    def _lock_dedup_key(self, task_path: str, dedup_key: dict[str, Any]) -> None:
        """Hold a transaction-scoped advisory lock on the key, so a concurrent enqueue of the same key waits for
        this one's row to commit instead of missing it and inserting its own."""
        if connection.vendor != "postgresql":
            return
        digest = hashlib.blake2b(json.dumps([task_path, dedup_key], sort_keys=True).encode(), digest_size=8).digest()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int.from_bytes(digest, signed=True)])

    def _get_existing_task_result(
        self, task: DeduplicatingTask[P, T], dedup_key: dict[str, Any]
    ) -> DBTaskResult | None:
        """
        Fetch a ready or running task matching the dedup key, or a successful one when ``dedup_successful``.

        Args:
            task: Task instance being enqueued.
            dedup_key: The key from :meth:`_dedup_key`.

        Returns:
            The matching DB task result, or None if not found.
        """
        if task.dedup_on is None:
            key_filter = Q(args_kwargs=dedup_key)
        else:
            key_filter = Q()
            for name, value in dedup_key.items():
                lookup = f"args_kwargs__kwargs__{name}"
                # An omitted kwarg has no JSON key, an explicit None is JSON null: both are the same call.
                key_filter &= (
                    Q(**{lookup: value})
                    if value is not None
                    else Q(**{f"{lookup}__isnull": True}) | Q(**{lookup: None})
                )
        statuses = [TaskResultStatus.READY, TaskResultStatus.RUNNING]
        if task.dedup_successful:
            statuses.append(TaskResultStatus.SUCCESSFUL)
        return DBTaskResult.objects.filter(
            key_filter, backend_name=self.alias, task_path=task.module_path, status__in=statuses
        ).first()
