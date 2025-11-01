import asyncio
import functools
import inspect
from typing import TYPE_CHECKING, Any

from django.core.cache import caches
from django.db import close_old_connections, connections

from asgiref.sync import ThreadSensitiveContext
from celery import Celery, signals
from langchain_core.tracers.langchain import wait_for_all_tracers

if TYPE_CHECKING:
    from collections.abc import Callable

app = Celery("daiv")

app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


def _async_to_sync_wrapper(async_func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Wraps async functions for Celery tasks with proper connection cleanup.

    Credits: https://mrdonbrown.blogspot.com/2025/10/using-async-functions-in-celery-with.html
    """

    @functools.wraps(async_func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        async def wrapped_with_context() -> Any:
            try:
                # Close stale connections BEFORE task execution
                close_old_connections()

                async with ThreadSensitiveContext():
                    return await async_func(*args, **kwargs)
            finally:
                # Close connections AFTER task execution
                close_old_connections()

        # Check if we're already in an event loop (e.g., during tests with CELERY_TASK_ALWAYS_EAGER)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running, create a new one (normal Celery worker behavior)
            return asyncio.run(wrapped_with_context())
        else:
            # Already in an event loop (test scenario)
            # Return the coroutine directly so it can be awaited by the async test
            return wrapped_with_context()

    # Preserve function signature for inspection
    sync_wrapper.__signature__ = inspect.signature(async_func)
    sync_wrapper.__annotations__ = async_func.__annotations__
    return sync_wrapper


def async_task(**kwargs):
    """Custom task decorator that supports async functions."""

    def inner(func):
        # Detect async functions and wrap them
        if inspect.iscoroutinefunction(func):
            func = _async_to_sync_wrapper(func)

        return app.task(**kwargs)(func)

    return inner


@signals.worker_process_init.connect
def init_worker_process(**kwargs: Any) -> None:
    """Close all connections inherited from parent process during prefork."""
    # Close all database connections inherited from parent
    for conn in connections.all():
        conn.close()

    # Also close cache connections
    for cache in caches.all():
        if hasattr(cache, "close"):
            cache.close()


@signals.worker_process_shutdown.connect
def shutdown_worker_process(**kwargs: Any) -> None:
    """Close all connections when worker process shuts down."""
    for conn in connections.all():
        conn.close()


@signals.task_postrun.connect
def flush_after_tasks(**kwargs):
    wait_for_all_tracers()
