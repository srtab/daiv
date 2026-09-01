from datetime import timedelta

from django.utils import timezone

import pytest
from sessions.models import Run, RunStatus, Session, SessionOrigin, WatchState
from sessions.pipeline_watch.store import WatchStore

from ..conftest import amake_watched_session


async def _make(thread_id, **kwargs):
    """The shared watch row, identified by ``thread_id`` alone — these tests select on the watch
    columns, never on a merge request."""
    kwargs.setdefault("merge_request_iid", None)
    return await amake_watched_session(thread_id=thread_id, **kwargs)


def _a_non_watch_origin() -> str:
    return next(origin for origin in SessionOrigin if origin != SessionOrigin.PIPELINE_WEBHOOK)


@pytest.mark.django_db(transaction=True)
async def test_the_transition_primitive_reports_the_single_winner():
    """Four call sites branch on this return value to decide whether to comment."""
    await _make("contended", watch_state=WatchState.WATCHING)
    store = WatchStore()

    first = await store.atransition("contended", expect=WatchState.WATCHING, watch_state=WatchState.EXHAUSTED)
    second = await store.atransition("contended", expect=WatchState.WATCHING, watch_state=WatchState.GREEN)

    assert (first, second) == (True, False)
    session = await Session.objects.aget(thread_id="contended")
    assert session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_the_stuck_sweep_only_moves_a_row_still_being_fixed():
    """The sweep selects FIXING and then writes; if the fix run lands GREEN in between, an
    unguarded write resurrects a closed watch."""
    await _make("moved-on", watch_state=WatchState.GREEN)

    won = await WatchStore().atransition(
        "moved-on", expect=WatchState.FIXING, watch_state=WatchState.WATCHING, watch_armed_at=timezone.now()
    )

    assert won is False
    session = await Session.objects.aget(thread_id="moved-on")
    assert session.watch_state == WatchState.GREEN


@pytest.mark.django_db(transaction=True)
async def test_a_set_of_expected_states_is_accepted():
    """``aexhaust`` leaves from either open state, so the primitive takes a set as well as one."""
    await _make("either", watch_state=WatchState.FIXING)

    won = await WatchStore().atransition("either", expect=WatchState.open(), watch_state=WatchState.EXHAUSTED)

    assert won is True


@pytest.mark.django_db(transaction=True)
async def test_the_open_watch_read_prefers_the_most_recently_armed():
    """Two sessions can share a branch; the watch that matters is the one armed last."""
    now = timezone.now()
    await _make("old", watch_state=WatchState.WATCHING, watch_armed_at=now - timedelta(hours=1))
    await _make("new", watch_state=WatchState.WATCHING, watch_armed_at=now)

    session = await WatchStore().aopen_watch("group/repo", "daiv/branch")

    assert session.thread_id == "new"


@pytest.mark.django_db(transaction=True)
async def test_a_closed_watch_is_not_open():
    await _make("closed", watch_state=WatchState.GREEN)
    store = WatchStore()

    assert await store.aopen_watch("group/repo", "daiv/branch") is None
    assert await store.ahas_open_watch("group/repo", "daiv/branch") is False


@pytest.mark.django_db(transaction=True)
async def test_a_refund_reopens_the_watch_and_returns_the_attempt():
    await _make("refund", watch_state=WatchState.FIXING, watch_attempts=2, watch_pipeline_id=7)

    await WatchStore().arefund_attempt("refund")

    session = await Session.objects.aget(thread_id="refund")
    assert (session.watch_state, session.watch_attempts, session.watch_pipeline_id) == (WatchState.WATCHING, 1, None)


@pytest.mark.django_db(transaction=True)
async def test_a_refund_never_touches_a_watch_that_moved_on():
    await _make("refund-late", watch_state=WatchState.GREEN, watch_attempts=2)

    await WatchStore().arefund_attempt("refund-late")

    session = await Session.objects.aget(thread_id="refund-late")
    assert (session.watch_state, session.watch_attempts) == (WatchState.GREEN, 2)


@pytest.mark.django_db(transaction=True)
async def test_a_run_with_no_row_is_not_a_fix_run():
    """Every fix-run dispatcher must thread its ``run_id`` through here or the loop bound is lost."""
    store = WatchStore()
    assert await store.ais_fix_run(None) is False


@pytest.mark.django_db(transaction=True)
async def test_a_pipeline_webhook_run_is_a_fix_run():
    session = await _make("owner")
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )
    other = await Run.objects.acreate(
        session=session, trigger_type=_a_non_watch_origin(), repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )
    store = WatchStore()

    assert await store.ais_fix_run(str(run.pk)) is True
    assert await store.ais_fix_run(str(other.pk)) is False
