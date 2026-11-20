"""Whether a task worker process is still alive.

``db_worker`` (``core.management.commands.db_worker``) refreshes a cache key under its worker id from a thread of its
own, apart from the task it runs, so a task whose event loop is blocked or whose database writes fail still has a live
worker; a killed worker's key expires.
"""

import logging
import threading
import time

from django.core.cache import cache

logger = logging.getLogger("daiv.core")

REFRESH_INTERVAL_S = 30.0


def start(worker_id: str) -> None:
    """Refresh ``worker_id``'s key every ``REFRESH_INTERVAL_S`` for as long as this process lives."""
    threading.Thread(target=_refresh_forever, args=(worker_id,), name="worker-liveness", daemon=True).start()


def refresh(worker_id: str) -> None:
    cache.set(_key(worker_id), True, timeout=3 * REFRESH_INTERVAL_S)


def alive(worker_id: str) -> bool:
    """Whether ``worker_id`` refreshed its key lately; a failed read is logged and counts as alive, so only a definite
    answer declares a worker dead."""
    try:
        return cache.get(_key(worker_id)) is not None
    except Exception:
        logger.exception("Could not read whether worker_id=%s is alive", worker_id)
        return True


def claimant_alive(worker_ids: list[str]) -> bool:
    """Whether the worker that last claimed a task is alive; a task no worker claimed has none."""
    return bool(worker_ids) and alive(worker_ids[-1])


def _refresh_forever(worker_id: str) -> None:
    while True:
        try:
            refresh(worker_id)
        except Exception:
            logger.exception("Could not refresh the liveness of worker_id=%s", worker_id)
        time.sleep(REFRESH_INTERVAL_S)


def _key(worker_id: str) -> str:
    return f"worker-alive:{worker_id}"
