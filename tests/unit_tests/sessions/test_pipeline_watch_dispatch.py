"""How a fix run is dispatched, and the wire that carries the attempt counter across it.

``_adispatch_fix_run`` used to enqueue first and create the Run afterwards, so ``run_job_task``
had no ``run_id`` to read ``trigger_type`` off — the arm saw "not a fix run" and reset
``watch_attempts`` to 0 on every re-arm, leaving the loop unbounded. Everything here is about
that wire rather than about the helper's own arguments.
"""

from django.utils import timezone

import pytest
from asgiref.sync import sync_to_async
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.models import Scope as SandboxScope
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch import _adispatch_fix_run

from codebase.base import Scope
from codebase.repo_config import RepositoryConfig
from codebase.utils import compute_thread_id

from .conftest import make_pipeline

MR_IID = 91


@pytest.fixture
def stub_enqueue(monkeypatch):
    """Capture the enqueued ``run_job_task`` kwargs, returning a real task-result row to link."""
    calls: list[dict] = []
    holder: dict = {"result": None}

    class FakeTask:
        async def aenqueue(self, **kwargs):
            calls.append(kwargs)
            return holder["result"]

    monkeypatch.setattr("jobs.tasks.run_job_task", FakeTask())
    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())
    return calls, holder


async def _make_watched_session(*, attempts: int = 0) -> Session:
    return await Session.objects.acreate(
        thread_id=compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=MR_IID),
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=MR_IID,
        watch_state=WatchState.FIXING,
        watch_attempts=attempts,
        watch_armed_at=timezone.now(),
    )


@pytest.mark.django_db(transaction=True)
async def test_the_fix_run_row_exists_before_its_task_and_carries_the_run_id(stub_enqueue, create_db_task_result):
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session()

    await _adispatch_fix_run(session=session, pipeline=make_pipeline(), repo_id="group/repo", merge_request_iid=MR_IID)

    run = await Run.objects.aget(session_id=session.thread_id)
    assert calls[0]["run_id"] == str(run.pk)
    assert run.trigger_type == SessionOrigin.PIPELINE_WEBHOOK
    # QUEUED means "not yet enqueued" everywhere else, and both recovery sweeps promote such a
    # row and enqueue it — a second task for one attempt.
    assert run.status == RunStatus.READY
    assert run.task_result_id == holder["result"].id


@pytest.mark.django_db(transaction=True)
async def test_the_attempt_counter_survives_the_wire_from_dispatch_to_re_arm(
    stub_enqueue, create_db_task_result, monkeypatch
):
    """The whole loop guard in one pass: dispatch, then the ``trigger_type`` read
    ``_ais_fix_run`` does off the Run row, then the arm that consumes it."""
    from sessions.pipeline_watch import _ais_fix_run, aarm_watch_after_run

    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session(attempts=2)

    class FakeEvaluate:
        async def aenqueue(self, **kwargs):
            pass

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeEvaluate())

    await _adispatch_fix_run(session=session, pipeline=make_pipeline(), repo_id="group/repo", merge_request_iid=MR_IID)

    assert await _ais_fix_run(calls[0]["run_id"]) is True

    await aarm_watch_after_run(
        repo_id="group/repo",
        run_id=calls[0]["run_id"],
        merge_request={"merge_request_id": MR_IID, "source_branch": "daiv/branch"},
        published=True,
    )

    await session.arefresh_from_db()
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 2


@pytest.mark.django_db(transaction=True)
async def test_the_fix_run_resolves_a_sandbox_environment(stub_enqueue, create_db_task_result):
    """The run that most needs to execute the repo's test suite must not land on the site default
    by omission — both MR-comment callbacks resolve an env and thread it through."""
    calls, holder = stub_enqueue
    holder["result"] = await sync_to_async(create_db_task_result)()
    session = await _make_watched_session()
    env = await SandboxEnvironment.objects.acreate(
        scope=SandboxScope.GLOBAL, name="ci", base_image="python:3.14", repo_ids=["group/repo"]
    )

    await _adispatch_fix_run(session=session, pipeline=make_pipeline(), repo_id="group/repo", merge_request_iid=MR_IID)

    run = await Run.objects.aget(session_id=session.thread_id)
    assert calls[0]["sandbox_environment_id"] == str(env.id)
    assert str(run.sandbox_environment_id) == str(env.id)


@pytest.mark.django_db(transaction=True)
async def test_a_failed_enqueue_refunds_the_attempt_and_reopens_the_watch(stub_enqueue, monkeypatch):
    """The claim charges the attempt before the dispatch, so a broker failure would otherwise
    spend it on nothing and leave the row in ``fixing`` until the 30-minute stale sweep."""
    calls, _holder = stub_enqueue

    class BrokenTask:
        async def aenqueue(self, **kwargs):
            raise RuntimeError("broker down")

    monkeypatch.setattr("jobs.tasks.run_job_task", BrokenTask())
    session = await _make_watched_session(attempts=2)

    await _adispatch_fix_run(session=session, pipeline=make_pipeline(), repo_id="group/repo", merge_request_iid=MR_IID)

    await session.arefresh_from_db()
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 1
    assert session.watch_pipeline_id is None
    # The orphan row is reachable by neither arm of sync_stuck_runs, so it must not be left READY.
    run = await Run.objects.aget(session_id=session.thread_id)
    assert run.status == RunStatus.FAILED
