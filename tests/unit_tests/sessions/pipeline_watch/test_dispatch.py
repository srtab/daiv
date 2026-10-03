"""How a fix run is dispatched, and the wire that carries the attempt counter across it.

``FixRunDispatcher.adispatch`` used to enqueue first and create the Run afterwards, so ``run_job_task``
had no ``run_id`` to read ``trigger_type`` off — the arm saw "not a fix run" and reset
``watch_attempts`` to 0 on every re-arm, leaving the loop unbounded. Everything here is about
that wire rather than about the helper's own arguments.
"""

import pytest
from asgiref.sync import sync_to_async
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.models import Scope as SandboxScope
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.dispatch import FixRunDispatcher
from sessions.pipeline_watch.judgment import PipelineReport

from codebase.base import Scope
from codebase.utils import compute_thread_id

from ..conftest import amake_watched_session, make_pipeline

MR_IID = 91
MR_THREAD = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=MR_IID)


@pytest.fixture
def stub_enqueue(monkeypatch):
    """Capture the enqueued ``run_job_task`` kwargs, returning a real task-result row to link."""
    calls: list[dict] = []
    holder: dict = {"result": None}

    class FakeTask:
        async def aenqueue(self, **kwargs):
            calls.append(kwargs)
            return holder["result"]

    monkeypatch.setattr("sessions.executor.tasks.run_job_task", FakeTask())
    return calls, holder


@pytest.fixture
def stub_evaluate(monkeypatch):
    """Swallow the evaluation an arm enqueues."""

    class FakeEvaluate:
        async def aenqueue(self, **kwargs):
            pass

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeEvaluate())


async def _make_watched_session(*, attempts: int = 0, sandbox_environment=None) -> Session:
    """A watch already claimed by this dispatch — the row state ``adispatch`` is called against."""
    return await amake_watched_session(
        thread_id=MR_THREAD,
        merge_request_iid=MR_IID,
        watch_state=WatchState.FIXING,
        watch_attempts=attempts,
        sandbox_environment=sandbox_environment,
    )


@pytest.mark.django_db(transaction=True)
async def test_the_fix_run_row_exists_before_its_task_and_carries_the_run_id(stub_enqueue, create_db_task_result):
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session()

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    run = await Run.objects.aget(session_id=session.thread_id)
    assert calls[0]["run_id"] == str(run.pk)
    assert run.trigger_type == SessionOrigin.PIPELINE_WEBHOOK
    # QUEUED means "not yet enqueued" everywhere else, and both recovery sweeps promote such a
    # row and enqueue it — a second task for one attempt.
    assert run.status == RunStatus.READY
    assert run.task_result_id == holder["result"].id


@pytest.mark.django_db(transaction=True)
async def test_fix_run_on_webhook_session_is_not_authenticated(stub_enqueue, create_db_task_result):
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session()

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    assert calls[0]["acting_user_authenticated"] is False


@pytest.mark.django_db(transaction=True)
async def test_fix_run_on_chat_session_is_authenticated(stub_enqueue, create_db_task_result):
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await amake_watched_session(
        thread_id=MR_THREAD, merge_request_iid=MR_IID, watch_state=WatchState.FIXING, origin=SessionOrigin.CHAT
    )

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    assert calls[0]["acting_user_authenticated"] is True


@pytest.mark.django_db(transaction=True)
async def test_the_attempt_counter_survives_the_wire_from_dispatch_to_re_arm(
    stub_enqueue, stub_evaluate, create_db_task_result
):
    """The whole loop guard in one pass: dispatch, then the ``trigger_type`` read
    ``WatchStore.ais_fix_run`` does off the Run row, then the arm that consumes it."""
    from sessions.pipeline_watch.service import PipelineWatch
    from sessions.pipeline_watch.store import WatchStore

    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session(attempts=2)

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    assert await WatchStore().ais_fix_run(calls[0]["run_id"]) is True

    await PipelineWatch("group/repo").aarm_after_run(
        run_id=calls[0]["run_id"],
        merge_request={"merge_request_id": MR_IID, "source_branch": "daiv/branch"},
        published=True,
    )

    await session.arefresh_from_db()
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 2


@pytest.mark.django_db(transaction=True)
async def test_the_fix_run_keeps_the_sessions_environment_after_the_repo_binding_moves(
    stub_enqueue, create_db_task_result
):
    """The fix run redoes the session's work, so it runs where the session did, not where the repo points now."""
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    original = await SandboxEnvironment.objects.acreate(
        scope=SandboxScope.GLOBAL, name="ci", base_image="python:3.14", repo_ids=["group/repo"]
    )
    session = await _make_watched_session(sandbox_environment=original)
    original.repo_ids = []
    await original.asave(update_fields=["repo_ids"])
    await SandboxEnvironment.objects.acreate(
        scope=SandboxScope.GLOBAL, name="ci-next", base_image="python:3.14-slim", repo_ids=["group/repo"]
    )

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    run = await Run.objects.aget(session_id=session.thread_id)
    assert calls[0]["sandbox_environment_id"] == str(original.id)
    assert str(run.sandbox_environment_id) == str(original.id)


@pytest.mark.django_db(transaction=True)
async def test_a_fix_run_runs_in_the_environment_of_the_run_that_published_the_mr(
    stub_enqueue, stub_evaluate, create_db_task_result
):
    """The arm, not the publishing run, creates the MR thread, so the environment reaches the fix run only through it.
    The environment binds no repository: a fresh match would miss it."""
    from sessions.pipeline_watch.service import PipelineWatch

    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    picked = await SandboxEnvironment.objects.acreate(
        scope=SandboxScope.GLOBAL, name="picked", base_image="python:3.14"
    )

    await PipelineWatch("group/repo").aarm_after_run(
        run_id=None,
        merge_request={"merge_request_id": MR_IID, "source_branch": "daiv/branch"},
        published=True,
        sandbox_environment_id=str(picked.id),
    )
    session = await Session.objects.aget(thread_id=MR_THREAD)

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    assert calls[0]["sandbox_environment_id"] == str(picked.id)


@pytest.mark.django_db(transaction=True)
async def test_a_session_without_an_environment_runs_its_fix_on_the_global_default(stub_enqueue, create_db_task_result):
    """A session with no environment recorded (its publishing run had none, or it was deleted since) leaves the
    choice to the GLOBAL default, even when an environment binds the repo now."""
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session()
    await SandboxEnvironment.objects.acreate(
        scope=SandboxScope.GLOBAL, name="ci", base_image="python:3.14", repo_ids=["group/repo"]
    )

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    run = await Run.objects.aget(session_id=session.thread_id)
    assert calls[0]["sandbox_environment_id"] is None
    assert run.sandbox_environment_id is None


@pytest.mark.django_db(transaction=True)
async def test_a_failed_enqueue_refunds_the_attempt_and_reopens_the_watch(stub_enqueue, monkeypatch):
    """The claim charges the attempt before the dispatch, so a broker failure would otherwise
    spend it on nothing and leave the row in ``fixing`` until the 30-minute stale sweep."""
    calls, _holder = stub_enqueue

    class BrokenTask:
        async def aenqueue(self, **kwargs):
            raise RuntimeError("broker down")

    monkeypatch.setattr("sessions.executor.tasks.run_job_task", BrokenTask())
    session = await _make_watched_session(attempts=2)

    await FixRunDispatcher().adispatch(
        session=session, report=PipelineReport(make_pipeline()), repo_id="group/repo", merge_request_iid=MR_IID
    )

    await session.arefresh_from_db()
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 1
    assert session.watch_pipeline_id is None
    # The orphan row is reachable by neither arm of sync_stuck_runs, so it must not be left READY.
    run = await Run.objects.aget(session_id=session.thread_id)
    assert run.status == RunStatus.FAILED
