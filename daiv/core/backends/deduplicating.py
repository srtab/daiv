from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from django.db import transaction

from django_tasks.base import Task, TaskResultStatus
from django_tasks.utils import normalize_json
from django_tasks_db import DatabaseBackend
from django_tasks_db.models import DBTaskResult

if TYPE_CHECKING:
    from django_tasks_db.backend import TaskResult as DatabaseTaskResult

logger = logging.getLogger("daiv.tasks")

T = TypeVar("T")
P = ParamSpec("P")


class DeduplicatingDatabaseBackend(DatabaseBackend):
    """
    Database backend that skips enqueuing duplicate tasks based on module path, args, and kwargs.
    """

    def enqueue(self, task: Task[P, T], args: P.args, kwargs: P.kwargs) -> DatabaseTaskResult[T]:
        """
        Enqueue a task unless a matching dedup key is already queued.

        Returns the existing TaskResult when a duplicate is detected.

        Args:
            task: Task instance being enqueued.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            The task result for the new or existing task.
        """
        existing_result = self._get_existing_task_result(task.module_path, args, kwargs)
        if existing_result:
            logger.info("Skipping duplicate task: %s with args: %r and kwargs: %r", task.module_path, args, kwargs)
            return existing_result.task_result

        return super().enqueue(task, args, kwargs)

    @transaction.atomic
    def _get_existing_task_result(self, task_path: str, args: P.args, kwargs: P.kwargs) -> DBTaskResult | None:
        """
        Fetch the oldest ready or running task matching the dedup key.

        Args:
            task_path: Fully-qualified task path.
            args: Positional arguments for the task.
            kwargs: Keyword arguments for the task.

        Returns:
            The matching DB task result, or None if not found.
        """
        return (
            DBTaskResult.objects
            .filter(
                backend_name=self.alias,
                task_path=task_path,
                args_kwargs=normalize_json({"args": args, "kwargs": kwargs}),
                status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING, TaskResultStatus.SUCCESSFUL],
            )
            .select_for_update()
            .first()
        )
