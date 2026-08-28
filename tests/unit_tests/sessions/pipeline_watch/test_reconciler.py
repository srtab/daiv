from django.utils import timezone

import pytest
from sessions.models import Session, SessionOrigin, WatchState
from sessions.pipeline_watch.platform import WatchPlatform
from sessions.pipeline_watch.service import WATCH_MAX_AGE, WATCH_STALE_AFTER, PipelineWatch, areconcile_watches


@pytest.mark.django_db(transaction=True)
async def test_a_stale_watching_session_is_re_evaluated(monkeypatch):
    await Session.objects.acreate(
        thread_id="stale",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=7,
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now() - WATCH_STALE_AFTER * 2,
    )
    evaluated = []

    async def fake_evaluate(self, **kwargs):
        evaluated.append(kwargs)

    monkeypatch.setattr(PipelineWatch, "aevaluate", fake_evaluate)
    touched = await areconcile_watches()

    assert touched == 1
    # pipeline_id is None so the reconciler polls for the latest, rather than re-reading
    # a pipeline it was never told about.
    assert evaluated[0]["pipeline_id"] is None


@pytest.mark.django_db(transaction=True)
async def test_a_fresh_watch_is_left_alone(monkeypatch):
    await Session.objects.acreate(
        thread_id="fresh",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now(),
    )
    evaluated = []

    async def fake_evaluate(self, **kwargs):
        evaluated.append(kwargs)

    monkeypatch.setattr(PipelineWatch, "aevaluate", fake_evaluate)
    assert await areconcile_watches() == 0
    assert evaluated == []


@pytest.mark.django_db(transaction=True)
async def test_a_watch_past_its_lifetime_is_abandoned(monkeypatch):
    await Session.objects.acreate(
        thread_id="ancient",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2,
    )

    async def fake_evaluate(self, **kwargs):
        raise AssertionError("an expired watch must not be evaluated")

    monkeypatch.setattr(PipelineWatch, "aevaluate", fake_evaluate)
    await areconcile_watches()

    session = await Session.objects.aget(thread_id="ancient")
    assert session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_an_expired_watch_says_so_on_the_merge_request(monkeypatch):
    """The expiry is where every unresolved failure lands, so closing it silently is what made an
    outage, a misconfigured cap and a pipeline that never started all look identical."""
    comments = []

    async def fake_comment(self, **kwargs):
        comments.append(kwargs)

    monkeypatch.setattr(WatchPlatform, "apost_note", fake_comment)
    monkeypatch.setattr(PipelineWatch, "aevaluate", lambda self, **kw: _noop())

    await Session.objects.acreate(
        thread_id="ancient2",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=11,
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2,
    )

    await areconcile_watches()

    assert len(comments) == 1
    assert comments[0]["merge_request_iid"] == 11


@pytest.mark.django_db(transaction=True)
async def test_an_expired_watch_is_logged_per_session(monkeypatch, caplog):
    """The only signal was an aggregate count with no repo and no thread, so nobody could
    reconstruct which watch gave up or why."""
    monkeypatch.setattr(WatchPlatform, "apost_note", lambda self, **kw: _noop())
    monkeypatch.setattr(PipelineWatch, "aevaluate", lambda self, **kw: _noop())

    await Session.objects.acreate(
        thread_id="ancient3",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        merge_request_iid=12,
        watch_state=WatchState.WATCHING,
        watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2,
    )

    with caplog.at_level("WARNING"):
        await areconcile_watches()

    assert "ancient3" in caplog.text or "group/repo" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.django_db(transaction=True)
async def test_a_stuck_fixing_watch_is_recovered(monkeypatch):
    await Session.objects.acreate(
        thread_id="stuck",
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.FIXING,
        watch_armed_at=timezone.now() - WATCH_STALE_AFTER * 2,
    )
    monkeypatch.setattr(PipelineWatch, "aevaluate", lambda self, **kw: _noop())
    await areconcile_watches()

    session = await Session.objects.aget(thread_id="stuck")
    # The fix run died; go back to watching so the next pipeline (or sweep) is judged.
    assert session.watch_state == WatchState.WATCHING


async def _noop():
    return None
