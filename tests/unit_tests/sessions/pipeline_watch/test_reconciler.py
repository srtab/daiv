from django.utils import timezone

import pytest
from sessions.models import Session, WatchState
from sessions.pipeline_watch.reconciler import WATCH_MAX_AGE, WATCH_STALE_AFTER, WatchReconciler

from ..conftest import amake_watched_session


def _reconciler(*, evaluate=None, notes=None):
    """A reconciler whose per-repo collaborators are recorded rather than real."""

    class _Watch:
        def __init__(self, repo_id):
            self.repo_id = repo_id

        async def aevaluate(self, **kwargs):
            if evaluate is not None:
                await evaluate(repo_id=self.repo_id, **kwargs)

    class _Platform:
        def __init__(self, repo_id):
            self.repo_id = repo_id

        async def apost_note(self, **kwargs):
            if notes is not None:
                notes.append({"repo_id": self.repo_id, **kwargs})

    return WatchReconciler(watch_factory=_Watch, platform_factory=_Platform)


@pytest.mark.django_db(transaction=True)
async def test_a_stale_watching_session_is_re_evaluated():
    await amake_watched_session(thread_id="stale", watch_armed_at=timezone.now() - WATCH_STALE_AFTER * 2)
    evaluated = []

    async def fake_evaluate(**kwargs):
        evaluated.append(kwargs)

    touched = await _reconciler(evaluate=fake_evaluate).areconcile()

    assert touched == 1
    # pipeline_id is None so the reconciler polls for the latest, rather than re-reading
    # a pipeline it was never told about.
    assert evaluated[0]["pipeline_id"] is None


@pytest.mark.django_db(transaction=True)
async def test_a_fresh_watch_is_left_alone():
    await amake_watched_session(thread_id="fresh")
    evaluated = []

    async def fake_evaluate(**kwargs):
        evaluated.append(kwargs)

    assert await _reconciler(evaluate=fake_evaluate).areconcile() == 0
    assert evaluated == []


@pytest.mark.django_db(transaction=True)
async def test_a_watch_past_its_lifetime_is_abandoned():
    await amake_watched_session(thread_id="ancient", watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2)

    async def fake_evaluate(**kwargs):
        raise AssertionError("an expired watch must not be evaluated")

    await _reconciler(evaluate=fake_evaluate).areconcile()

    session = await Session.objects.aget(thread_id="ancient")
    assert session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_an_expired_watch_says_so_on_the_merge_request():
    """The expiry is where every unresolved failure lands, so closing it silently is what made an
    outage, a misconfigured cap and a pipeline that never started all look identical."""
    comments = []

    await amake_watched_session(
        thread_id="ancient2", merge_request_iid=11, watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2
    )

    await _reconciler(notes=comments).areconcile()

    assert len(comments) == 1
    assert comments[0]["merge_request_iid"] == 11


@pytest.mark.django_db(transaction=True)
async def test_an_expired_watch_is_logged_per_session(caplog):
    """The only signal was an aggregate count with no repo and no thread, so nobody could
    reconstruct which watch gave up or why."""
    await amake_watched_session(
        thread_id="ancient3", merge_request_iid=12, watch_armed_at=timezone.now() - WATCH_MAX_AGE * 2
    )

    with caplog.at_level("WARNING"):
        await _reconciler().areconcile()

    assert "ancient3" in caplog.text or "group/repo" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.django_db(transaction=True)
async def test_a_stuck_fixing_watch_is_recovered():
    await amake_watched_session(
        thread_id="stuck", watch_state=WatchState.FIXING, watch_armed_at=timezone.now() - WATCH_STALE_AFTER * 2
    )
    await _reconciler().areconcile()

    session = await Session.objects.aget(thread_id="stuck")
    # The fix run died; go back to watching so the next pipeline (or sweep) is judged.
    assert session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_each_repository_gets_its_own_collaborators():
    """The sweep spans repositories, so a single shared client would address the wrong one."""
    for n, repo in enumerate(["group/a", "group/b"]):
        await amake_watched_session(
            thread_id=f"stale-{n}", repo_id=repo, watch_armed_at=timezone.now() - WATCH_STALE_AFTER * 2
        )
    seen = []

    async def fake_evaluate(**kwargs):
        seen.append(kwargs["repo_id"])

    await _reconciler(evaluate=fake_evaluate).areconcile()

    assert sorted(seen) == ["group/a", "group/b"]
