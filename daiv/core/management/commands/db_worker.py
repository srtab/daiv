from __future__ import annotations

from typing import TYPE_CHECKING

from django_tasks_db.management.commands import db_worker as upstream

from core import worker_liveness, worker_shutdown

if TYPE_CHECKING:
    from types import FrameType


class Worker(upstream.Worker):
    """django_tasks_db's worker, which also tells the task it is running that the worker is shutting down
    (:mod:`core.worker_shutdown`) and keeps saying it is alive (:mod:`core.worker_liveness`)."""

    def run(self) -> None:
        worker_liveness.start(self.worker_id)
        super().run()

    def shutdown(self, signum: int, frame: FrameType | None) -> None:
        worker_shutdown.request()
        super().shutdown(signum, frame)


class Command(upstream.Command):
    """django_tasks_db's ``db_worker``, run with :class:`Worker`. It takes the command's name, as ``core`` comes before
    ``django_tasks_db`` in ``INSTALLED_APPS``."""

    def handle(self, *args, **options) -> None:
        # ``upstream.Command.handle`` builds its worker from this module global.
        upstream_worker, upstream.Worker = upstream.Worker, Worker  # ty: ignore[invalid-assignment]
        try:
            super().handle(*args, **options)
        finally:
            upstream.Worker = upstream_worker
