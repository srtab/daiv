import uuid

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.models import Run, RunStatus, Session, SessionOrigin

from accounts.models import User


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


@pytest.fixture
def admin_user(db):
    return User.objects.create_user(
        username="admin",
        email="admin@test.com",
        password="testpass123",  # noqa: S106
        role="admin",
    )


@pytest.fixture
def session_fixture(admin_user):
    return Session.objects.create(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/project", ref="main", user=admin_user
    )


@pytest.fixture
def run_fixture(session_fixture):
    return Run.objects.create(
        session=session_fixture,
        trigger_type=SessionOrigin.UI_JOB,
        repo_id=session_fixture.repo_id,
        status=RunStatus.SUCCESSFUL,
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="other",
        email="other@test.com",
        password="testpass123",  # noqa: S106
        role="member",
    )
