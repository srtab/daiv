import uuid

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max


@pytest.fixture
def create_db_task_result():
    """Build a DBTaskResult row for signal / view / command tests."""

    def _create(
        *,
        status="SUCCESSFUL",
        return_value=None,
        started_at=None,
        finished_at=None,
        exception_class_path="",
        traceback="",
    ):
        return DBTaskResult.objects.create(
            id=uuid.uuid4(),
            status=status,
            task_path="jobs.tasks.run_job_task",
            args_kwargs={"args": [], "kwargs": {}},
            queue_name="default",
            backend_name="default",
            run_after=get_date_max(),
            return_value=return_value or {},
            started_at=started_at,
            finished_at=finished_at,
            exception_class_path=exception_class_path,
            traceback=traceback,
        )

    return _create


@pytest.fixture(autouse=True)
def _cleanup_sessions_rows(django_db_blocker):
    """Delete committed Session/Run rows after each test.

    Some sessions tests use ``@pytest.mark.django_db(transaction=True)`` or
    async DB access (which commits via a separate connection) and those rows
    are not rolled back by pytest-django's savepoint/transaction mechanism.
    Without this cleanup they leak into later tests (e.g. global-count
    assertions). Runs for every test — for purely transaction-wrapped tests it
    is a harmless no-op.
    """
    yield
    with django_db_blocker.unblock():
        import threading

        from django.db import close_old_connections

        def _delete() -> None:
            import logging

            from sessions.models import Run, Session

            # Runs on its own thread so it gets a fresh autocommit DB connection:
            # the deletes then commit unconditionally instead of being rolled back
            # with an enclosing atomic block on the main (test) thread.
            close_old_connections()
            try:
                Run.objects.all().delete()
                Session.objects.all().delete()
            except Exception:
                logging.getLogger("tests").warning("session-row cleanup failed", exc_info=True)
            finally:
                close_old_connections()

        t = threading.Thread(target=_delete)
        t.start()
        t.join()
