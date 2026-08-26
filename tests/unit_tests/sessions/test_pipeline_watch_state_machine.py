import asyncio
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

import pytest
from sessions.models import Session, SessionOrigin, WatchState
from sessions.pipeline_watch import (
    WATCH_STALE_AFTER,
    aarm_watch,
    aevaluate_watch,
    aexhaust_watch,
    watch_enabled,
    watch_max_attempts,
)

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings

from .conftest import make_job, make_pipeline


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
    """Capture dispatches and serve a canned pipeline, with the MR head sha the poll correlates on."""
    calls = {"dispatched": [], "comments": [], "pipeline": None, "head_sha": "abc123"}

    async def fake_dispatch(**kwargs):
        calls["dispatched"].append(kwargs)

    async def fake_comment(**kwargs):
        calls["comments"].append(kwargs)

    async def fake_read(**kwargs):
        # A real yield point, so concurrent evaluations both get past the read before
        # either reaches the compare-and-swap.
        await asyncio.sleep(0)
        return calls["pipeline"]

    class FakeClient:
        def get_merge_request(self, repo_id, merge_request_id):
            return SimpleNamespace(sha=calls["head_sha"])

    monkeypatch.setattr("sessions.pipeline_watch._adispatch_fix_run", fake_dispatch)
    monkeypatch.setattr("sessions.pipeline_watch._apost_watch_note", fake_comment)
    monkeypatch.setattr("sessions.pipeline_watch._aread_pipeline", fake_read)
    monkeypatch.setattr("sessions.pipeline_watch.RepoClient.create_instance", lambda **_kw: FakeClient())
    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())
    return calls


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
async def test_a_pipeline_that_has_not_finished_leaves_the_watch_armed(watched_session, stub_watch):
    """The arm-time evaluation runs seconds after the publish push, so the pipeline is still
    running. ``judge_pipeline`` calls that UNCLEAR, and acting on it closed every watch DAIV armed.
    """
    stub_watch["pipeline"] = make_pipeline("running")
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=None)

    assert stub_watch["dispatched"] == []
    assert stub_watch["comments"] == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING
    assert watched_session.watch_pipeline_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", ["blocked", "manual"])
async def test_a_pipeline_waiting_on_a_human_still_stops_the_watch_with_a_note(watched_session, stub_watch, status):
    """These are settled states, not slow ones: nothing resolves them without a person, so the
    "not judgeable yet" gate must not swallow them into the six-hour expiry."""
    jobs = [make_job("manual", name="deploy")]
    stub_watch["pipeline"] = make_pipeline(status, pipeline_id=250, jobs=jobs)
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=250)

    assert stub_watch["dispatched"] == []
    assert len(stub_watch["comments"]) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_a_pipeline_that_does_not_exist_yet_leaves_the_watch_armed(watched_session, stub_watch):
    """WATCH_MAX_AGE is the backstop for a pipeline that never starts, not an MR comment."""
    stub_watch["pipeline"] = None
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=None)

    assert stub_watch["comments"] == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_from_an_earlier_push_is_ignored(watched_session, stub_watch):
    """The poll reads *the latest* pipeline for the ref, which correlates with no particular push:
    a stale ``success`` would close the watch green, a stale ``failed`` spend an attempt twice."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=400)
    stub_watch["head_sha"] = "deadbee"
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=None)

    assert stub_watch["dispatched"] == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_at_the_branch_head_is_judged(watched_session, stub_watch):
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=401)
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=None)

    assert len(stub_watch["dispatched"]) == 1


@pytest.mark.django_db(transaction=True)
async def test_the_head_sha_guard_does_not_gate_the_webhook_path(watched_session, stub_watch):
    """A webhook names the pipeline it is reporting, so correlation is already established."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=402)
    stub_watch["head_sha"] = "deadbee"
    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=402)

    assert len(stub_watch["dispatched"]) == 1


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_already_acted_on_is_not_re_dispatched(watched_session, stub_watch):
    """Invariant 4 on the poll path: the reconciler passes no pipeline id, so the dedupe has to
    compare the pipeline it actually read."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=300)
    watched_session.watch_pipeline_id = 300
    await watched_session.asave()

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=None)
    assert stub_watch["dispatched"] == []


@pytest.mark.django_db(transaction=True)
async def test_a_second_concurrent_evaluation_does_not_dispatch(watched_session, stub_watch):
    """Invariant 3. Two workflows finishing together (or a webhook retry) both read ``watching``
    with the same count and both clear the cap check, so only the CAS can pick a winner."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=500)

    await asyncio.gather(
        aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=500),
        aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=500),
    )

    assert len(stub_watch["dispatched"]) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_attempts == 1


@pytest.mark.django_db(transaction=True)
async def test_two_concurrent_exhaustions_comment_and_notify_once(watched_session, stub_watch, monkeypatch):
    """Same race as the dispatch claim, but on the terminal branches: both readers clear the cap
    check off their own snapshot, and the comment and the notification are both non-idempotent."""
    notified = []

    async def fake_notify(**kwargs):
        notified.append(kwargs)

    monkeypatch.setattr("sessions.pipeline_watch.anotify_watch_exhausted", fake_notify)
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=800)
    await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(watch_attempts=3)

    await asyncio.gather(
        aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=800),
        aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=800),
    )

    assert len(stub_watch["comments"]) == 1
    assert len(notified) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_the_cap_is_re_asserted_when_the_claim_lands(watched_session, stub_watch, monkeypatch):
    """A reader's cap check runs off a snapshot that can outlive a whole fix-run cycle — the row
    reaches the cap and is re-armed while it waits — so the CAS has to bound the count itself
    rather than trust the value that cleared the check."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=850)

    async def read_then_spend_the_whole_budget(**kwargs):
        await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(
            watch_attempts=3, watch_state=WatchState.WATCHING
        )
        return stub_watch["pipeline"]

    monkeypatch.setattr("sessions.pipeline_watch._aread_pipeline", read_then_spend_the_whole_budget)

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=850)

    await watched_session.arefresh_from_db()
    assert stub_watch["dispatched"] == []
    assert watched_session.watch_attempts == 3


@pytest.mark.django_db(transaction=True)
async def test_arming_a_new_watch_records_the_publishing_user(monkeypatch, django_user_model):
    """The MR thread has no session until the publish, so this call creates it — and a row created
    without a user gets every fix run unattributed: no notification, no user-tier env, no MCP."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr("sessions.pipeline_watch.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig())
    user = await django_user_model.objects.acreate(username="publisher", email="publisher@example.com")

    armed = await aarm_watch(
        repo_id="group/repo", merge_request_iid=74, ref="daiv/branch", was_fix_run=False, user_id=user.pk
    )

    assert armed == compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=74)
    session = await Session.objects.aget(thread_id=armed)
    assert session.user_id == user.pk


@pytest.mark.django_db(transaction=True)
async def test_dispatching_a_fix_run_restamps_the_staleness_clock(watched_session, stub_watch):
    """WATCH_STALE_AFTER is measured off ``watch_armed_at``; a watch armed before a slow CI run
    would otherwise hand the reconciler a ``fixing`` row that is already stale."""
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=600)
    armed_at = timezone.now() - timedelta(minutes=45)
    await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(watch_armed_at=armed_at)

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=600)

    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.FIXING
    assert watched_session.watch_armed_at > armed_at + WATCH_STALE_AFTER


@pytest.mark.django_db(transaction=True)
async def test_a_repo_cannot_raise_the_cap_above_the_site_value(watched_session, stub_watch, monkeypatch):
    """The docs promise a repo can only tighten, but ``max_attempts`` is a plain int whose default
    comes from site settings — an explicit ``.daiv.yml`` value replaces it rather than clamping."""
    monkeypatch.setattr(site_settings, "pipeline_watch_max_attempts", 2)
    monkeypatch.setattr(
        "sessions.pipeline_watch.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}}),
    )
    stub_watch["pipeline"] = make_pipeline("failed", pipeline_id=700)
    watched_session.watch_attempts = 2
    await watched_session.asave()

    await aevaluate_watch(repo_id="group/repo", ref="daiv/branch", pipeline_id=700)

    assert stub_watch["dispatched"] == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


def test_the_attempt_cap_is_clamped_to_the_site_value(monkeypatch):
    monkeypatch.setattr(site_settings, "pipeline_watch_max_attempts", 2)
    assert watch_max_attempts(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}})) == 2
    assert watch_max_attempts(RepositoryConfig(**{"pipeline_watch": {"max_attempts": 1}})) == 1


def test_a_repo_cannot_enable_a_watch_the_operator_turned_off(monkeypatch):
    monkeypatch.setattr(site_settings, "pipeline_watch_enabled", False)
    assert watch_enabled(RepositoryConfig(**{"pipeline_watch": {"enabled": True}})) is False

    monkeypatch.setattr(site_settings, "pipeline_watch_enabled", True)
    assert watch_enabled(RepositoryConfig(**{"pipeline_watch": {"enabled": False}})) is False
    assert watch_enabled(RepositoryConfig()) is True


@pytest.mark.django_db(transaction=True)
async def test_a_disabled_site_switch_stops_the_watch_from_arming(monkeypatch):
    monkeypatch.setattr(site_settings, "pipeline_watch_enabled", False)
    monkeypatch.setattr(
        "sessions.pipeline_watch.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"enabled": True}}),
    )
    assert await aarm_watch(repo_id="group/repo", merge_request_iid=81, ref="daiv/branch", was_fix_run=False) is None


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
    armed = await aarm_watch(repo_id="group/repo", merge_request_iid=71, ref="daiv/branch", was_fix_run=False)
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
    await aarm_watch(repo_id="group/repo", merge_request_iid=72, ref="daiv/branch", was_fix_run=True)
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
