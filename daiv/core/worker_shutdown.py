"""Whether this process's task worker was asked to shut down.

``db_worker`` (``core.management.commands.db_worker``) requests it when it gets SIGTERM, SIGINT or SIGQUIT. The
worker then lets its current task finish before it exits, so a task that runs for long, like an agent run, asks
:func:`requested` and stops early, ending cleanly within the deployment's stop grace period instead of being
killed mid-run.
"""

import threading

_requested = threading.Event()


def request() -> None:
    _requested.set()


def requested() -> bool:
    return _requested.is_set()
