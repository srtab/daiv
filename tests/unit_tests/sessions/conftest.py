import inspect
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.files.base import ContentFile
from django.utils import timezone

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.artifacts import guess_content_type
from sessions.models import (
    Run,
    RunArtifact,
    RunStatus,
    Session,
    SessionArchive,
    SessionOrigin,
    WatchState,
    artifact_upload_to,
)
from sessions.signal_defs import run_finished

from accounts.models import User
from codebase.base import Job, MergeRequest, Pipeline
from codebase.base import User as PlatformUser
from core.site_settings import site_settings
from tests.unit_tests.conftest import site_snapshot

if TYPE_CHECKING:
    from datetime import datetime


def make_artifact(
    run: Run,
    *,
    filename: str = "report.md",
    content: bytes = b"# Report",
    title: str = "",
    created_at: datetime | None = None,
) -> RunArtifact:
    """A ``RunArtifact`` whose bytes are in the default storage, shaped as ``astore_artifact`` stores it. SYNC ONLY."""
    artifact = RunArtifact(
        run=run,
        title=title or filename,
        filename=filename,
        content_type=guess_content_type(filename),
        size=len(content),
    )
    if created_at is not None:
        artifact.created_at = created_at
    artifact.file.save(filename, ContentFile(content), save=True)
    return artifact


def commit_revision(loaded: RunArtifact, content: bytes) -> str:
    """Revise ``loaded``'s row behind its reader, as a concurrent publish would; returns the new name. SYNC ONLY."""
    storage = loaded.file.storage
    revised = storage.save(artifact_upload_to(loaded, loaded.filename, revision="rev"), ContentFile(content))
    RunArtifact.objects.filter(pk=loaded.pk).update(file=revised, size=len(content))
    storage.delete(loaded.file.name)
    return revised


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
            task_path="sessions.executor.tasks.run_job_task",
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


def make_run(*, session_id: str, status: str, trigger_type: str = SessionOrigin.API_JOB, **kwargs) -> Run:
    """A run on the session ``session_id``, which is created on first use. SYNC ONLY."""
    session = Session.objects.get_or_create(
        thread_id=session_id, defaults={"origin": trigger_type, "repo_id": "acme/api"}
    )[0]
    return Run.objects.create(
        session=session, trigger_type=trigger_type, repo_id="acme/api", status=status, prompt="p", **kwargs
    )


class FakeRun:
    """The ``Run`` a patched ``sessions.services.acreate_run`` returns: the columns ``aenqueue_run`` reads."""

    def __init__(self, **kwargs):
        self.id = self.pk = uuid.uuid4()
        self.task_result_id = kwargs.get("task_result_id")
        self.session_id = kwargs.get("thread_id") or str(uuid.uuid4())
        self.trigger_type = kwargs.get("trigger_type", "")
        self.repo_id = kwargs.get("repo_id", "")
        self.ref = kwargs.get("ref", "")
        self.prompt = kwargs.get("prompt", "")
        self.agent_model = kwargs.get("agent_model", "")
        self.agent_thinking_level = kwargs.get("agent_thinking_level", "")
        self.sandbox_environment_id = kwargs.get("sandbox_environment_id")
        self.user_id = user.pk if (user := kwargs.get("user")) is not None else None
        self.status = RunStatus.READY
        self.error_message = ""
        self.started_at = self.finished_at = None
        self.asave = AsyncMock(return_value=None)


async def fake_acreate_run(**kwargs) -> FakeRun:
    return FakeRun(**kwargs)


@pytest.fixture
def run_finished_spy():
    """A receiver connected to ``run_finished`` for the test."""
    spy = MagicMock()
    run_finished.connect(spy, dispatch_uid="tests-run-finished-spy")
    yield spy
    run_finished.disconnect(dispatch_uid="tests-run-finished-spy")


@pytest.fixture
def badge():
    """The nav badge refresh, ``ui_events.publisher.runs_changed``, patched."""
    with patch("core.ui_events.publisher.runs_changed") as publish:
        yield publish


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
    watch_pipeline_id: int | None = None,
    user: User | None = None,
    sandbox_environment=None,
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
        watch_pipeline_id=watch_pipeline_id,
        user=user,
        sandbox_environment=sandbox_environment,
    )


async def amake_job_session(thread_id: str | None = None, **fields) -> str:
    """Create a job ``Session`` row and return its thread id."""
    thread_id = thread_id or str(uuid.uuid4())
    await Session.objects.acreate(thread_id=thread_id, origin=SessionOrigin.UI_JOB, repo_id="owner/repo", **fields)
    return thread_id


async def active_holder(thread_id: str) -> str | None:
    return (await Session.objects.aget(thread_id=thread_id)).active_run_id


def watch_recorder(armed: list[dict], *, error: Exception | None = None):
    """A stand-in for ``PipelineWatch`` that records the ``aarm_after_run`` calls a seam makes.

    Patched over the name each seam imported, so it pins that seam's wiring — which arguments reach
    the watch — rather than the watch itself. Every publishing seam has such a test; keeping one
    stub means a new keyword argument is one edit, not three, and a copy that drifts records
    nothing while still passing. ``error`` is raised after each call is recorded. Calls are bound to the real
    signature, so a renamed argument fails here rather than in production's logged-and-swallowed arm.
    """
    from sessions.pipeline_watch.service import PipelineWatch

    class RecordingWatch:
        def __init__(self, repo_id):
            self.repo_id = repo_id

        async def aarm_after_run(self, **kwargs):
            inspect.signature(PipelineWatch.aarm_after_run).bind(self, **kwargs)
            armed.append({"repo_id": self.repo_id, **kwargs})
            if error is not None:
                raise error

    return RecordingWatch


@pytest.fixture
def site_setting():
    """Override a site setting as ``site_settings.snapshot()`` serves it, which is how the watch policy reads it."""
    overrides: dict[str, object] = {}

    def _set(name: str, value: object) -> None:
        overrides[name] = value

    with patch.object(site_settings, "snapshot", side_effect=lambda: site_snapshot(**overrides)):
        yield _set


def _archive_fields(epoch: int, messages: list, *, closed: bool = False, unreadable: bool = False, **values) -> dict:
    from sessions.archive import archive_row

    fields = {
        "epoch": epoch,
        "closed_at": timezone.now() if closed else None,
        **archive_row({"messages": messages, **values}),
    }
    return {**fields, "state": b"not zlib"} if unreadable else fields


def create_archive(session_id: str, epoch: int, messages: list, **options) -> SessionArchive:
    """A ``SessionArchive`` row holding ``messages`` and ``values``: closed as ``/clear`` leaves it when ``closed``, its
    state no longer decompressing when ``unreadable``."""
    return SessionArchive.objects.create(session_id=session_id, **_archive_fields(epoch, messages, **options))


async def acreate_archive(session_id: str, epoch: int, messages: list, **options) -> SessionArchive:
    """Async :func:`create_archive`."""
    return await SessionArchive.objects.acreate(session_id=session_id, **_archive_fields(epoch, messages, **options))


def fake_checkpointer(values: dict | None = None, *, error: Exception | None = None) -> SimpleNamespace:
    """A checkpointer whose thread holds ``values``, or no checkpoint for ``None``; ``error`` makes its read raise."""
    checkpoint = None if values is None else SimpleNamespace(checkpoint={"channel_values": values})
    return SimpleNamespace(aget_tuple=AsyncMock(return_value=checkpoint, side_effect=error))


def make_merge_request() -> MergeRequest:
    return MergeRequest(
        repo_id="group/repo",
        merge_request_id=7,
        source_branch="daiv/fix",
        target_branch="main",
        title="Fix",
        description="",
        labels=["daiv"],
        web_url="https://gitlab.example/group/repo/-/merge_requests/7",
        sha="b4c9f488efdc044b39083ff605b3920c4d842e41",
        author=PlatformUser(id=2, name="DAIV", username="daiv"),
        draft=False,
    )
