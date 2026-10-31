import inspect
import json
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from django.core.files.base import ContentFile
from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount
from django_tasks_db.models import DBTaskResult, get_date_max
from langchain_core.messages import AIMessage, ToolMessage
from sessions.artifacts import guess_content_type
from sessions.models import (
    MergeRequestOwnership,
    Run,
    RunArtifact,
    RunStatus,
    Session,
    SessionArchive,
    SessionOrigin,
    artifact_upload_to,
)
from sessions.signal_defs import run_finished
from sessions.turns import tool_call_segment

from accounts.models import User
from codebase.base import Job, MergeRequest, Pipeline
from codebase.base import User as PlatformUser
from codebase.models import RepositoryAccess
from core.site_settings import site_settings
from tests.unit_tests.conftest import site_snapshot

if TYPE_CHECKING:
    from datetime import datetime


def grant_read_access(user, repo_id: str) -> None:
    """SYNC ONLY: the platform identity and fresh ``RepositoryAccess`` row ``visible_to`` reads."""
    SocialAccount.objects.get_or_create(user=user, provider="gitlab", uid=str(user.pk))
    RepositoryAccess.objects.create(
        provider="gitlab",
        uid=str(user.pk),
        username=user.username,
        repo_id=repo_id,
        access_level="read",
        synced_at=timezone.now(),
    )


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


def make_task_result(
    *, status="SUCCESSFUL", return_value=None, started_at=None, finished_at=None, exception_class_path="", traceback=""
) -> DBTaskResult:
    """SYNC ONLY: a ``run_job_task`` DBTaskResult row."""
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


@pytest.fixture
def create_db_task_result():
    """Build a DBTaskResult row for signal / view / command tests."""
    return make_task_result


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


async def amake_owned_merge_request(
    *,
    thread_id: str = "mr-thread",
    repo_id: str = "group/repo",
    ref: str = "daiv/branch",
    merge_request_iid: int = 7,
    user: User | None = None,
    **fields,
) -> MergeRequestOwnership:
    """A session that owns an open merge request on ``ref`` — the starting row for every CI watch test."""
    session = await Session.objects.acreate(
        thread_id=thread_id, origin=SessionOrigin.CHAT, repo_id=repo_id, ref=ref, user=user
    )
    return await MergeRequestOwnership.objects.acreate(
        repo_id=repo_id, merge_request_iid=merge_request_iid, session=session, owner=user, source_branch=ref, **fields
    )


async def amake_job_session(thread_id: str | None = None, **fields) -> str:
    """Create a job ``Session`` row and return its thread id."""
    thread_id = thread_id or str(uuid.uuid4())
    await Session.objects.acreate(thread_id=thread_id, origin=SessionOrigin.UI_JOB, repo_id="owner/repo", **fields)
    return thread_id


async def active_holder(thread_id: str) -> str | None:
    return (await Session.objects.aget(thread_id=thread_id)).active_run_id


def claim_recorder(claimed: list[dict], *, error: Exception | None = None):
    """A stand-in for ``aclaim_ownership`` that records the claims a seam makes.

    Patched over the name each seam imported, so it pins that seam's wiring — which arguments reach the claim —
    rather than the claim itself. Calls are bound to the real signature, so a renamed argument fails here rather than
    in production's logged-and-swallowed claim. ``error`` is raised after each call is recorded. It claims nothing, so
    the seam enqueues no CI evaluation.
    """
    from sessions.inbox.ownership import aclaim_ownership

    async def _claim(**kwargs):
        inspect.signature(aclaim_ownership).bind(**kwargs)
        claimed.append(kwargs)
        if error is not None:
            raise error

    return _claim


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


def make_coordinator(user=None, **fields) -> Session:
    defaults = {"origin": SessionOrigin.API_JOB, "repo_id": "acme/coord", "ref": "main", "user": user}
    return Session.objects.create(thread_id=str(uuid.uuid4()), **(defaults | fields))


def make_delegating_run(coordinator: Session, user) -> Run:
    return Run.objects.create(
        session=coordinator,
        trigger_type=SessionOrigin.API_JOB,
        repo_id=coordinator.repo_id,
        status=RunStatus.RUNNING,
        user=user,
    )


def make_leg(
    coordinator: Session, batch_id: uuid.UUID, *, status: str, task_result=None, session_ref: str = "", **fields
) -> Run:
    session = Session.objects.create(
        thread_id=str(uuid.uuid4()),
        origin=SessionOrigin.DELEGATED_JOB,
        repo_id=fields.get("repo_id", "acme/api"),
        ref=session_ref,
        parent=coordinator,
    )
    fields = {"user": coordinator.user, "finished_at": timezone.now()} | fields
    return Run.objects.create(
        session=session,
        trigger_type=SessionOrigin.DELEGATED_JOB,
        batch_id=batch_id,
        repo_id=session.repo_id,
        status=status,
        task_result=task_result,
        **{k: v for k, v in fields.items() if k != "repo_id"},
    )


def delegate_segment(batch_id: uuid.UUID, result: str | None = None, *, goal: str = "g") -> dict:
    segment = tool_call_segment({"id": "d1", "name": "delegate_jobs", "args": {"goal": goal, "targets": []}})
    segment["result"] = result if result is not None else json.dumps({"batch_id": str(batch_id), "started": []})
    return segment


def delegate_messages(batch_id: uuid.UUID) -> list:
    return [
        AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "delegate_jobs", "args": {"goal": "g"}}]),
        ToolMessage(content=json.dumps({"batch_id": str(batch_id), "started": []}), tool_call_id="t1", id="m1"),
    ]


def delegate_turns(*batch_ids: uuid.UUID, goal: str = "g") -> list[dict]:
    return [{"role": "assistant", "segments": [delegate_segment(batch_id, goal=goal) for batch_id in batch_ids]}]
