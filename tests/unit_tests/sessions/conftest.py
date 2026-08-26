import uuid

from django.utils import timezone

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState

from accounts.models import User
from codebase.base import Job, Pipeline


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


def make_job(status: str = "failed", *, name: str = "tests", allow_failure: bool = False) -> Job:
    return Job(id=1, name=name, status=status, stage="test", allow_failure=allow_failure)


def make_pipeline(status: str = "failed", jobs: list[Job] | None = None, *, pipeline_id: int = 100) -> Pipeline:
    """``jobs=None`` synthesizes one job matching ``status``; pass ``[]`` for a jobless pipeline."""
    return Pipeline(
        id=pipeline_id,
        iid=1,
        sha="abc123",
        status=status,
        web_url=f"https://example.com/p/{pipeline_id}",
        jobs=[make_job(status)] if jobs is None else jobs,
    )


async def amake_watched_session(
    *,
    thread_id: str = "mr-thread",
    repo_id: str = "group/repo",
    ref: str = "daiv/branch",
    merge_request_iid: int | None = 7,
    watch_state: str = WatchState.WATCHING,
    watch_attempts: int = 0,
    watch_armed_at=None,
) -> Session:
    """A session with the watch armed — the starting row for every watch test."""
    return await Session.objects.acreate(
        thread_id=thread_id,
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id=repo_id,
        ref=ref,
        merge_request_iid=merge_request_iid,
        watch_state=watch_state,
        watch_attempts=watch_attempts,
        watch_armed_at=watch_armed_at or timezone.now(),
    )
