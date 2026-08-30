import asyncio
from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

import pytest
from sessions.models import Session, SessionOrigin, WatchState
from sessions.pipeline_watch.dispatch import FixRunDispatcher
from sessions.pipeline_watch.notifier import WatchNotifier
from sessions.pipeline_watch.reconciler import WATCH_STALE_AFTER
from sessions.pipeline_watch.service import PipelineWatch

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings

from ..conftest import make_job, make_pipeline


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


class FakePlatform:
    """Stands in for the git platform. ``head_sha`` is what the correlation compares against."""

    def __init__(self):
        self.pipeline = None
        self.head_sha = "abc123"
        self.notes = []
        self.read_error = None
        self.on_read = None
        self.client_error = None
        self.correlated = []

    async def aensure_client(self):
        if self.client_error is not None:
            raise self.client_error

    async def aread_pipeline(self, *, ref, pipeline_id):
        # A real yield point, so concurrent evaluations both get past the read before
        # either reaches the compare-and-swap.
        await asyncio.sleep(0)
        if self.on_read is not None:
            await self.on_read()
        if self.read_error is not None:
            raise self.read_error
        return self.pipeline

    async def ais_head_pipeline(self, *, merge_request_iid, report):
        self.correlated.append(merge_request_iid)
        if not merge_request_iid:
            return False
        return bool(self.head_sha) and report.sha == self.head_sha

    async def apost_note(self, *, merge_request_iid, body):
        self.notes.append({"merge_request_iid": merge_request_iid, "body": body})


class RecordingDispatcher:
    def __init__(self):
        self.dispatched = []

    async def adispatch(self, **kwargs):
        self.dispatched.append(kwargs)


class RecordingNotifier:
    def __init__(self):
        self.notified = []

    async def anotify_exhausted(self, **kwargs):
        self.notified.append(kwargs)


@pytest.fixture
def watch(monkeypatch):
    """A ``PipelineWatch`` with the outside world faked and the store running for real."""
    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )
    platform, dispatcher, notifier = FakePlatform(), RecordingDispatcher(), RecordingNotifier()

    def make(repo_id: str = "group/repo") -> PipelineWatch:
        """A fresh instance per call: two concurrent events are two tasks and two instances,
        over one shared platform and one shared database."""
        return PipelineWatch(repo_id, platform=platform, dispatcher=dispatcher, notifier=notifier)

    return SimpleNamespace(make=make, platform=platform, dispatcher=dispatcher, notifier=notifier)


@pytest.mark.django_db(transaction=True)
async def test_a_real_failure_dispatches_a_fix_run(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed")
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)

    assert len(watch.dispatcher.dispatched) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.FIXING
    assert watched_session.watch_attempts == 1
    assert watched_session.watch_pipeline_id == 100


@pytest.mark.django_db(transaction=True)
async def test_an_uninjected_dispatcher_is_a_real_fix_run_dispatcher(watched_session, watch, monkeypatch):
    """Production injects no dispatcher, so the ``FixRunDispatcher(self._store)`` default in
    ``PipelineWatch.__init__`` is the only path it takes — and every other test here injects one.
    This pins that ``__init__`` builds the real ``FixRunDispatcher`` type (the ``isinstance``
    assertion below) and passes it the right keyword arguments; ``adispatch`` itself is
    monkeypatched away, so its body is not exercised here.
    """
    dispatched = []

    async def fake_dispatch(self, **kwargs):
        dispatched.append(kwargs)

    monkeypatch.setattr(FixRunDispatcher, "adispatch", fake_dispatch)
    pipeline = make_pipeline("failed", pipeline_id=900)
    watch.platform.pipeline = pipeline

    pw = PipelineWatch("group/repo", platform=watch.platform)
    assert isinstance(pw._dispatcher, FixRunDispatcher)
    # The store is shared deliberately: an injected store must also govern the refund.
    assert pw._dispatcher._store is pw._store

    await pw.aevaluate(ref="daiv/branch", pipeline_id=900)

    assert len(dispatched) == 1
    assert dispatched[0]["report"].pipeline is pipeline
    assert dispatched[0]["repo_id"] == "group/repo"
    assert dispatched[0]["merge_request_iid"] == 7
    assert dispatched[0]["session"].thread_id == watched_session.thread_id


@pytest.mark.django_db(transaction=True)
async def test_an_uninjected_notifier_is_a_real_watch_notifier(watched_session, watch, monkeypatch):
    """Production injects no notifier, so the ``WatchNotifier()`` default in ``PipelineWatch.__init__``
    is the only path it takes — and every other test here injects one. This pins that ``__init__``
    builds the real ``WatchNotifier`` type (the ``isinstance`` assertion below) and passes it the
    right keyword arguments; ``anotify_exhausted`` itself is monkeypatched away, so its body is not
    exercised here.
    """
    notified = []

    async def fake_notify(self, **kwargs):
        notified.append(kwargs)

    monkeypatch.setattr(WatchNotifier, "anotify_exhausted", fake_notify)
    pipeline = make_pipeline("failed", pipeline_id=901)
    watch.platform.pipeline = pipeline
    await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(watch_attempts=3)

    pw = PipelineWatch("group/repo", platform=watch.platform)
    assert isinstance(pw._notifier, WatchNotifier)

    await pw.aevaluate(ref="daiv/branch", pipeline_id=901)

    assert len(notified) == 1
    assert notified[0]["report"].pipeline is pipeline
    assert notified[0]["session"].thread_id == watched_session.thread_id


@pytest.mark.django_db(transaction=True)
async def test_a_green_pipeline_ends_the_watch(watched_session, watch):
    watch.platform.pipeline = make_pipeline("success")
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)

    assert watch.dispatcher.dispatched == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.GREEN


@pytest.mark.django_db(transaction=True)
async def test_an_unreadable_pipeline_stops_the_watch_with_a_note(watched_session, watch):
    watch.platform.pipeline = make_pipeline("success", jobs=[])
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)

    assert watch.dispatcher.dispatched == []
    assert len(watch.platform.notes) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_the_same_pipeline_is_only_acted_on_once(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed")
    watched_session.watch_pipeline_id = 100
    await watched_session.asave()

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)
    assert watch.dispatcher.dispatched == []


@pytest.mark.django_db(transaction=True)
async def test_a_session_being_fixed_ignores_new_events(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=200)
    watched_session.watch_state = WatchState.FIXING
    await watched_session.asave()

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=200)
    assert watch.dispatcher.dispatched == []


@pytest.mark.django_db(transaction=True)
async def test_at_the_cap_it_gives_up_instead_of_dispatching(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=200)
    watched_session.watch_attempts = 3
    await watched_session.asave()

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=200)

    assert watch.dispatcher.dispatched == []
    assert len(watch.platform.notes) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_a_pipeline_that_has_not_finished_leaves_the_watch_armed(watched_session, watch):
    """The arm-time evaluation runs seconds after the publish push, so the pipeline is still
    running. ``PipelineReport`` calls that UNCLEAR, and acting on it closed every watch DAIV armed.
    """
    watch.platform.pipeline = make_pipeline("running")
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=None)

    assert watch.dispatcher.dispatched == []
    assert watch.platform.notes == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING
    assert watched_session.watch_pipeline_id is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", ["blocked", "manual"])
async def test_a_pipeline_waiting_on_a_human_still_stops_the_watch_with_a_note(watched_session, watch, status):
    """These are settled states, not slow ones: nothing resolves them without a person, so the
    "not judgeable yet" gate must not swallow them into the six-hour expiry."""
    jobs = [make_job("manual", name="deploy")]
    watch.platform.pipeline = make_pipeline(status, pipeline_id=250, jobs=jobs)
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=250)

    assert watch.dispatcher.dispatched == []
    assert len(watch.platform.notes) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_a_pipeline_that_does_not_exist_yet_leaves_the_watch_armed(watched_session, watch):
    """WATCH_MAX_AGE is the backstop for a pipeline that never starts, not an MR comment."""
    watch.platform.pipeline = None
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=None)

    assert watch.platform.notes == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_from_an_earlier_push_is_ignored(watched_session, watch):
    """The poll reads *the latest* pipeline for the ref, which correlates with no particular push:
    a stale ``success`` would close the watch green, a stale ``failed`` spend an attempt twice."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=400)
    watch.platform.head_sha = "deadbee"
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=None)

    assert watch.dispatcher.dispatched == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_at_the_branch_head_is_judged(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=401)
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=None)

    assert len(watch.dispatcher.dispatched) == 1


@pytest.mark.django_db(transaction=True)
async def test_a_superseded_webhook_pipeline_is_ignored(watched_session, watch):
    """A webhook names *which* pipeline it reports, not *whether* that pipeline is still the head.
    GitLab auto-cancels redundant pipelines by default, so the push after ours makes the older one
    emit a terminal ``canceled`` — which judges UNCLEAR and would close the watch."""
    watch.platform.pipeline = make_pipeline("canceled", pipeline_id=402)
    watch.platform.head_sha = "deadbee"
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=402)

    assert watch.dispatcher.dispatched == []
    assert watch.platform.notes == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_a_webhook_pipeline_at_the_branch_head_is_judged(watched_session, watch):
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=403)
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=403)

    assert len(watch.dispatcher.dispatched) == 1


@pytest.mark.django_db(transaction=True)
async def test_an_unreadable_head_sha_does_not_close_the_watch(watched_session, watch):
    """Correlation is a precondition, not a verdict: a merge request with no head sha to compare
    against leaves the watch armed for the next event rather than judging an uncorrelated pipeline.
    The read that *raises* is covered in ``test_platform.py``."""
    watch.platform.head_sha = None
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=404)

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=404)

    assert watch.dispatcher.dispatched == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING


@pytest.mark.django_db(transaction=True)
async def test_two_concurrent_green_verdicts_close_the_watch_once(watched_session, watch):
    """Only the ACTIONABLE branch used to compare-and-swap, so the terminal branches acted on a
    stale read and each posted its own MR comment."""
    watch.platform.pipeline = make_pipeline("success", jobs=[])

    await asyncio.gather(
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=700),
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=700),
    )

    assert len(watch.platform.notes) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.UNCLEAR


@pytest.mark.django_db(transaction=True)
async def test_two_concurrent_give_ups_comment_and_notify_once(watched_session, watch):
    """Duplicate MR comments are a failure mode this project has already shipped once."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=701)
    watched_session.watch_attempts = 3
    await watched_session.asave()

    await asyncio.gather(
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=701),
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=701),
    )

    assert len(watch.platform.notes) == 1
    assert len(watch.notifier.notified) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_a_polled_pipeline_already_acted_on_is_not_re_dispatched(watched_session, watch):
    """Invariant 4 on the poll path: the reconciler passes no pipeline id, so the dedupe has to
    compare the pipeline it actually read."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=300)
    watched_session.watch_pipeline_id = 300
    await watched_session.asave()

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=None)
    assert watch.dispatcher.dispatched == []


@pytest.mark.django_db(transaction=True)
async def test_a_second_concurrent_evaluation_does_not_dispatch(watched_session, watch):
    """Invariant 3. Two workflows finishing together (or a webhook retry) both read ``watching``
    with the same count and both clear the cap check, so only the CAS can pick a winner."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=500)

    await asyncio.gather(
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=500),
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=500),
    )

    assert len(watch.dispatcher.dispatched) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_attempts == 1


@pytest.mark.django_db(transaction=True)
async def test_two_concurrent_exhaustions_comment_and_notify_once(watched_session, watch):
    """Same race as the dispatch claim, but on the terminal branches: both readers clear the cap
    check off their own snapshot, and the comment and the notification are both non-idempotent."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=800)
    await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(watch_attempts=3)

    await asyncio.gather(
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=800),
        watch.make().aevaluate(ref="daiv/branch", pipeline_id=800),
    )

    assert len(watch.platform.notes) == 1
    assert len(watch.notifier.notified) == 1
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_the_cap_is_re_asserted_when_the_claim_lands(watched_session, watch):
    """A reader's cap check runs off a snapshot that can outlive a whole fix-run cycle — the row
    reaches the cap and is re-armed while it waits — so the CAS has to bound the count itself
    rather than trust the value that cleared the check."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=850)

    async def spend_the_whole_budget():
        await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(
            watch_attempts=3, watch_state=WatchState.WATCHING
        )

    watch.platform.on_read = spend_the_whole_budget

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=850)

    await watched_session.arefresh_from_db()
    assert watch.dispatcher.dispatched == []
    assert watched_session.watch_attempts == 3


@pytest.mark.django_db(transaction=True)
async def test_arming_a_new_watch_records_the_publishing_user(watch, monkeypatch, django_user_model):
    """The MR thread has no session until the publish, so this call creates it — and a row created
    without a user gets every fix run unattributed: no notification, no user-tier env, no MCP."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )
    user = await django_user_model.objects.acreate(username="publisher", email="publisher@example.com")

    armed = await watch.make().aarm(merge_request_iid=74, ref="daiv/branch", was_fix_run=False, user_id=user.pk)

    assert armed == compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=74)
    session = await Session.objects.aget(thread_id=armed)
    assert session.user_id == user.pk


@pytest.mark.django_db(transaction=True)
async def test_dispatching_a_fix_run_restamps_the_staleness_clock(watched_session, watch):
    """WATCH_STALE_AFTER is measured off ``watch_armed_at``; a watch armed before a slow CI run
    would otherwise hand the reconciler a ``fixing`` row that is already stale."""
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=600)
    armed_at = timezone.now() - timedelta(minutes=45)
    await Session.objects.filter(thread_id=watched_session.thread_id).aupdate(watch_armed_at=armed_at)

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=600)

    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.FIXING
    assert watched_session.watch_armed_at > armed_at + WATCH_STALE_AFTER


@pytest.mark.django_db(transaction=True)
async def test_a_repo_cannot_raise_the_cap_above_the_site_value(watched_session, watch, monkeypatch, site_setting):
    """The docs promise a repo can only tighten, but ``max_attempts`` is a plain int whose default
    comes from site settings — an explicit ``.daiv.yml`` value replaces it rather than clamping."""
    site_setting("pipeline_watch_max_attempts", 2)
    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"max_attempts": 10}}),
    )
    watch.platform.pipeline = make_pipeline("failed", pipeline_id=700)
    watched_session.watch_attempts = 2
    await watched_session.asave()

    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=700)

    assert watch.dispatcher.dispatched == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_a_disabled_site_switch_stops_the_watch_from_arming(watch, monkeypatch, site_setting):
    site_setting("pipeline_watch_enabled", False)
    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config",
        lambda *_a, **_kw: RepositoryConfig(**{"pipeline_watch": {"enabled": True}}),
    )
    assert await watch.make().aarm(merge_request_iid=81, ref="daiv/branch", was_fix_run=False) is None


@pytest.mark.django_db(transaction=True)
async def test_arming_from_a_normal_run_resets_the_counter(watch, monkeypatch):
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )

    # The row must live at the MR thread id, or aarm creates a fresh session and the
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
    armed = await watch.make().aarm(merge_request_iid=71, ref="daiv/branch", was_fix_run=False)
    assert armed == mr_thread
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 0


@pytest.mark.django_db(transaction=True)
async def test_arming_from_a_fix_run_keeps_the_counter(watch, monkeypatch):
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )

    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=72)
    await Session.objects.acreate(
        thread_id=mr_thread,
        origin=SessionOrigin.MR_WEBHOOK,
        repo_id="group/repo",
        ref="daiv/branch",
        watch_state=WatchState.FIXING,
        watch_attempts=2,
    )
    await watch.make().aarm(merge_request_iid=72, ref="daiv/branch", was_fix_run=True)
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.WATCHING
    assert session.watch_attempts == 2


@pytest.mark.django_db(transaction=True)
async def test_a_fix_run_that_changed_nothing_gives_up(watch):
    """Invariant 7. No change means no push, which means no pipeline and no event — so this
    watch would sit in `fixing` until it aged out six hours later."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

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
    await watch.make().aexhaust(merge_request_iid=73, reason="the agent made no changes")
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.watch_state == WatchState.EXHAUSTED


@pytest.mark.django_db(transaction=True)
async def test_arming_gives_the_mr_thread_the_run_owner(watch, monkeypatch, django_user_model):
    """The MR thread is almost always created *here*, not by a human — so without an owner
    carried in, the give-up notification has no recipient and the fix run loses the user's
    personal MCP servers and USER-tier sandbox env."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )

    owner = await django_user_model.objects.acreate(username="runner", email="runner@example.com")
    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=91)

    armed = await watch.make().aarm(merge_request_iid=91, ref="daiv/branch", was_fix_run=False, user_id=owner.pk)

    assert armed == mr_thread
    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.user_id == owner.pk


@pytest.mark.django_db(transaction=True)
async def test_arming_adopts_an_ownerless_thread(watch, monkeypatch, django_user_model):
    """An MR thread created by an earlier webhook has no user; the first owned run must adopt it
    or that thread can never notify anyone."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )

    owner = await django_user_model.objects.acreate(username="runner2", email="runner2@example.com")
    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=92)
    await Session.objects.acreate(
        thread_id=mr_thread, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", ref="daiv/branch", user=None
    )

    await watch.make().aarm(merge_request_iid=92, ref="daiv/branch", was_fix_run=False, user_id=owner.pk)

    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.user_id == owner.pk


@pytest.mark.django_db(transaction=True)
async def test_arming_never_reassigns_an_owned_thread(watch, monkeypatch, django_user_model):
    """A human's MR conversation keeps its owner: a scheduled run publishing to the same MR must
    not silently move that thread (and its notifications) to the scheduler's user."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    monkeypatch.setattr(
        "sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )

    human = await django_user_model.objects.acreate(username="human2", email="human2@example.com")
    robot = await django_user_model.objects.acreate(username="robot", email="robot@example.com")
    mr_thread = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=93)
    await Session.objects.acreate(
        thread_id=mr_thread, origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", ref="daiv/branch", user=human
    )

    await watch.make().aarm(merge_request_iid=93, ref="daiv/branch", was_fix_run=False, user_id=robot.pk)

    session = await Session.objects.aget(thread_id=mr_thread)
    assert session.user_id == human.pk


@pytest.mark.django_db(transaction=True)
async def test_a_transient_read_failure_leaves_the_watch_armed_and_warns(watched_session, watch, caplog):
    """An unreadable pipeline must never become a verdict, and must never be silent either: the
    read used to answer ``None`` for an outage, which the watch logged at DEBUG and waited out."""
    from github import GithubException

    watch.platform.read_error = GithubException(403, None, None)

    with caplog.at_level("WARNING", logger="daiv.sessions"):
        await watch.make().aevaluate(ref="daiv/branch", pipeline_id=800)

    assert watch.dispatcher.dispatched == []
    assert watch.platform.notes == []
    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING
    # WARNING without a traceback: the watch polls for hours, so an outage must not mint one
    # Sentry error per sweep.
    records = [r for r in caplog.records if r.name == "daiv.sessions"]
    assert [r.levelname for r in records] == ["WARNING"]
    assert records[0].exc_info is None


@pytest.mark.django_db(transaction=True)
async def test_an_unexpected_read_failure_is_reported_with_a_traceback(watched_session, watch, caplog):
    watch.platform.read_error = AttributeError("'NoneType' object has no attribute 'sha'")

    with caplog.at_level("WARNING", logger="daiv.sessions"):
        await watch.make().aevaluate(ref="daiv/branch", pipeline_id=801)

    await watched_session.arefresh_from_db()
    assert watched_session.watch_state == WatchState.WATCHING
    records = [r for r in caplog.records if r.name == "daiv.sessions"]
    assert [r.levelname for r in records] == ["ERROR"]
    assert records[0].exc_info is not None


@pytest.mark.django_db(transaction=True)
async def test_an_unwatched_branch_never_buys_a_queue_round_trip(watch, monkeypatch):
    """CI fires on every branch of every repo and only a DAIV-published MR branch has a watch, so
    without the existence check each unrelated pipeline pays for a task to learn there is nothing
    to do."""
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeTask())

    assert await watch.make().arequest_evaluation(ref="someone-elses-branch", pipeline_id=1) is False
    assert enqueued == []


@pytest.mark.django_db(transaction=True)
async def test_a_watched_branch_enqueues_the_evaluation(watched_session, watch, monkeypatch):
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeTask())

    assert await watch.make().arequest_evaluation(ref="daiv/branch", pipeline_id=42) is True
    assert enqueued == [{"repo_id": "group/repo", "ref": "daiv/branch", "pipeline_id": 42}]


@pytest.mark.django_db(transaction=True)
async def test_arming_costs_one_site_settings_read(watch, monkeypatch):
    """``aarm`` runs at the end of every publishing chat turn, job and issue-addressor run, and
    reads only ``enabled``. Each ``site_settings`` read is a blocking thread hop, so a policy that
    resolves the attempt cap it never looks at doubles the cost of the whole arm path.
    """
    # Built before recording starts: its ``max_attempts`` default factory reads site settings too,
    # and in production that cost is paid once an hour by the config cache, not by the arm path.
    config = RepositoryConfig()
    monkeypatch.setattr("sessions.pipeline_watch.policy.RepositoryConfig.get_config", lambda *_a, **_kw: config)
    read = []
    original = type(site_settings).__getattr__

    def record(self, name):
        if name.startswith("pipeline_watch_"):
            read.append(name)
        return original(self, name)

    monkeypatch.setattr(type(site_settings), "__getattr__", record)

    await watch.make().aarm(merge_request_iid=81, ref="daiv/branch", was_fix_run=False)

    assert read == ["pipeline_watch_enabled"]


@pytest.mark.django_db(transaction=True)
async def test_a_client_that_cannot_be_built_is_not_a_ci_outage(watched_session, watch, caplog):
    """A client we cannot build is a deployment fault and must reach someone.

    ``is_transient_platform_error`` counts the 401/403 a dead GitHub App installation returns as
    transient, so resolving the client lazily inside the read guard would log a permanent
    misconfiguration at WARNING — which mints no Sentry event — on every webhook and every sweep,
    forever, while the user is told CI produced no result.
    """
    watch.platform.client_error = RuntimeError("no installation")
    watch.platform.pipeline = make_pipeline("failed")

    with pytest.raises(RuntimeError, match="no installation"):
        await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)

    assert "could not read the pipeline" not in caplog.text


@pytest.mark.django_db(transaction=True)
async def test_correlation_is_asked_about_the_watched_merge_request(watched_session, watch):
    """The head-correlation call is what decides whether a pipeline is acted on at all. Nothing else
    checks the iid reaching it, so a wrong one would correlate against another merge request and a
    ``None`` would silently skip every evaluation until the watch aged out.
    """
    watch.platform.pipeline = make_pipeline("failed")
    await watch.make().aevaluate(ref="daiv/branch", pipeline_id=100)

    assert watch.platform.correlated == [watched_session.merge_request_iid]
