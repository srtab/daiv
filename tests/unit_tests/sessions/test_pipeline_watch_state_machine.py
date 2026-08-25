from django.utils import timezone

import pytest
from sessions.models import Session, SessionOrigin, WatchState
from sessions.pipeline_watch import aarm_watch, aevaluate_watch, aexhaust_watch

from codebase.base import Job, Pipeline
from codebase.repo_config import RepositoryConfig


def make_pipeline(status: str, *, pipeline_id: int = 100, jobs=None) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        iid=1,
        sha="abc123",
        status=status,
        web_url="https://example.com/p/",
        jobs=jobs if jobs is not None else [Job(id=1, name="tests", status=status, stage="test", allow_failure=False)],
    )


@pytest.fixture
def watched_session(transactional_db):
    return Session.objects.create(
        thread_id="mr-thread",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=7,
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now(),
    )


@pytest.fixture
def stub_watch(monkeypatch):
    """Capture dispatches and serve a canned pipeline."""
    calls = {"dispatched": [], "comments": [], "pipeline": None}

    async def fake_dispatch(**kwargs):
        calls["dispatched"].append(kwargs)

    async def fake_comment(**kwargs):
        calls["comments"].append(kwargs)

    monkeypatch.setattr("sessions.pipeline_watch._adispatch_fix_run", fake_dispatch)
    monkeypatch.setattr("sessions.pipeline_watch._apost_watch_note", fake_comment)
    monkeypatch.setattr("sessions.pipeline_watch._aread_pipeline", lambda **kwargs: _async_return(calls["pipeline"]))
    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())
    return calls


def _async_return(value):
    async def _inner():
        return value

    return _inner()


@pytest.mark.django_db(transaction=True)
async def test_a_real_failure_dispatches_a_fix_run(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("failed")
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=100)

    assert len(stub_watch["dispatched"]) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.FIXING
    assert watched_session.watch_attempts == 1
    assert watched_session.watch_pipeline_id == 100


@pytest.mark.django_db(transaction=True)
async def test_a_green_pipeline_ends_the_watch(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("success")
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=100)

    assert stub_watch["dispatched"] == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.GREEN


@pytest.mark.django_db(transaction=True)
async def test_an_unreadable_pipeline_stops_the_watch_with_a_note(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("success", jobs=[])
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=100)

    assert stub_watch["dispatched"] == []
    assert len(stub_watch["comments"]) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_the_same_pipeline_is_only_acted_on_once(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("failed")
    watched_session.watch_pipeline_id = 100
    await watched_session.asave()

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=100)
    assert stub_watch["dispatched"] == []


@pytest.mark.django_db(transaction=True)
async def test_a_session_being_fixed_ignores_new_events(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=200)
    watched_session.watch_state = WatchState.FIXING
    await watched_session.asave()

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=200)
    assert stub_watch["dispatched"] == []


@pytest.mark.django_db(transaction=True)
async def test_at_the_cap_it_gives_up_instead_of_dispatching(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=200)
    watched_session.watch_attempts = 3
    await watched_session.asave()

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=200)

    assert stub_watch["dispatched"] == []
    assert len(stub_watch["comments"]) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_arming_from_a_normal_run_resets_the_counter(monkeypatch):
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())

    # The row must live at the MR thread id, or aarm_watch creates a fresh session and the
    # assertion passes without ever exercising the reset.
    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=71)
    await Session.objects.acreate(
        thread_id=mr_thread,
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.EXHAUSTED,
        watch_attempts=3,
    )
    armed = await aarm_watch(
        repo_id="group/repo",
        merge_request_iid=71,
        ref="daiv/branch",
        source_thread_id="some-other-thread",
        was_fix_run=False,
    )
    assert armed == mr_thread
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 0


@pytest.mark.django_db(transaction=True)
async def test_arming_from_a_fix_run_keeps_the_counter(monkeypatch):
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())

    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=72)
    await Session.objects.acreate(
        thread_id=mr_thread,
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.FIXING,
        watch_attempts=2,
    )
    await aarm_watch(
        repo_id="group/repo", merge_request_iid=72, ref="daiv/branch", source_thread_id=mr_thread, was_fix_run=True
    )
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 2


@pytest.mark.django_db(transaction=True)
async def test_a_fix_run_that_changed_nothing_gives_up(monkeypatch):
    """Invariant 7. No change means no push, which means no pipeline and no event — so this
    watch would sit in `fixing` until it aged out six hours later."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    async def fake_comment(**kwargs):
        pass

    monkeypatch.setattr("sessions.pipeline_watch._apost_watch_note", fake_comment)

    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=73)
    await Session.objects.acreate(
        thread_id=mr_thread,
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=73,
        watch_state=WatchState.FIXING,
        watch_attempts=1,
    )
    await aexhaust_watch(repo_id="group/repo", merge_request_iid=73, reason="the agent made no changes")
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.EXHAUSTED
