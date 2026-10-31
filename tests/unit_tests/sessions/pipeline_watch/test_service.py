import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from sessions.models import (
    EventKind,
    InboxEvent,
    MergeRequestOwnership,
    QuietReason,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.pipeline_watch.service import PipelineWatch
from sessions.signal_defs import watch_exhausted

from ..conftest import make_job, make_pipeline

pytestmark = pytest.mark.django_db(transaction=True)

REPO, BRANCH, SHA = "group/repo", "daiv/branch", "abc123"


class FakePlatform:
    """Stands in for the git platform. ``head_sha`` is what the correlation compares against."""

    def __init__(self):
        self.pipeline = None
        self.head_sha = SHA
        self.read_error = None
        self.reads = 0

    async def aensure_client(self):
        return None

    async def aread_pipeline(self, *, ref, pipeline_id):
        # A real yield point, so concurrent evaluations both get past the read before either records the pipeline.
        await asyncio.sleep(0)
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return self.pipeline

    async def ais_head_pipeline(self, *, merge_request_iid, report):
        return bool(merge_request_iid) and report.sha == self.head_sha


@pytest.fixture
def exhausted():
    received = []

    def receiver(sender, signal, **kwargs):
        received.append(kwargs)

    watch_exhausted.connect(receiver, weak=False, dispatch_uid="test-watch-exhausted")
    yield received
    watch_exhausted.disconnect(dispatch_uid="test-watch-exhausted")


@pytest.fixture(autouse=True)
def drain():
    with patch("sessions.inbox.post.drain_logged") as drain:
        yield drain


@pytest.fixture
def watch(exhausted):
    platform = FakePlatform()
    policy = SimpleNamespace(enabled=True, max_attempts=3)

    def make() -> PipelineWatch:
        return PipelineWatch(REPO, platform=platform, policy=policy)

    return SimpleNamespace(make=make, platform=platform, policy=policy, exhausted=exhausted)


async def _owned(**fields) -> MergeRequestOwnership:
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=REPO, ref=BRANCH, **fields.pop("session", {})
    )
    defaults = {
        "repo_id": REPO,
        "merge_request_iid": 7,
        "session": session,
        "source_branch": BRANCH,
        "last_daiv_sha": SHA,
    }
    return await MergeRequestOwnership.objects.acreate(**(defaults | fields))


async def _event() -> InboxEvent | None:
    return await InboxEvent.objects.afirst()


class TestVerdicts:
    async def test_a_green_pipeline_is_remembered_as_passing(self, watch):
        ownership = await _owned()
        watch.platform.pipeline = make_pipeline("success")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.session_id, event.kind, event.wake) == (ownership.session_id, EventKind.CI_PASSED, False)
        assert (event.source_key, event.subject, event.ref, event.merge_request_iid) == (
            "pipeline:100",
            "ci:7",
            BRANCH,
            7,
        )
        assert event.payload == {
            "pipeline_id": 100, "sha": SHA, "status": "success", "web_url": "https://example.com/p/100",
            "failed_jobs": [],
        }  # fmt: skip

    @pytest.mark.parametrize("status", ["manual", "canceled"])
    async def test_a_status_that_needs_a_person_is_remembered_as_stopped(self, watch, status):
        await _owned()
        watch.platform.pipeline = make_pipeline(status)

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.kind, event.wake) == (EventKind.CI_STOPPED, False)

    async def test_a_failure_on_daivs_commit_wakes_the_owning_session(self, watch, drain):
        ownership = await _owned()
        watch.platform.pipeline = make_pipeline("failed", [make_job("failed", name="image:build")])

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.kind, event.wake, event.reason) == (EventKind.CI_FAILED, True, "")
        assert event.payload["failed_jobs"] == ["image:build"]
        drain.assert_called_once_with(ownership.session_id)
        assert watch.exhausted == []

    async def test_a_blank_daiv_commit_counts_as_daivs(self, watch):
        await _owned(last_daiv_sha="")
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert (await _event()).wake is True


class TestWakeRule:
    async def test_the_watch_turned_off_for_the_merge_request(self, watch):
        await _owned(watch_enabled=False)

        assert await _quiet_reason(watch) == QuietReason.WATCH_OFF

    async def test_the_watch_turned_off_for_the_repository(self, watch):
        await _owned()
        watch.policy.enabled = False

        assert await _quiet_reason(watch) == QuietReason.WATCH_OFF

    async def test_a_muted_session(self, watch):
        await _owned(session={"inbox_muted": True})

        assert await _quiet_reason(watch) == QuietReason.MUTED

    async def test_a_commit_daiv_did_not_push(self, watch):
        await _owned(last_daiv_sha="0a1b2c3d")

        assert await _quiet_reason(watch) == QuietReason.NOT_DAIV_COMMIT

    async def test_a_commit_already_examined(self, watch):
        await _owned(last_examined_sha=SHA)

        assert await _quiet_reason(watch) == QuietReason.ALREADY_EXAMINED

    async def test_the_attempts_spent(self, watch):
        await _owned(attempts=3)

        assert await _quiet_reason(watch) == QuietReason.BUDGET_SPENT

    async def test_the_consecutive_run_limit(self, watch):
        ownership = await _owned()
        for _ in range(3):
            await Run.objects.acreate(
                session_id=ownership.session_id, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id=REPO,
                status=RunStatus.SUCCESSFUL,
            )  # fmt: skip

        assert await _quiet_reason(watch) == QuietReason.RUN_LIMIT


async def _quiet_reason(watch) -> str:
    watch.platform.pipeline = make_pipeline("failed")
    await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
    event = await _event()
    assert (event.kind, event.wake) == (EventKind.CI_FAILED, False)
    return event.reason


class TestNothingPosted:
    async def test_a_pipeline_that_is_not_the_head_posts_nothing(self, watch):
        await _owned()
        watch.platform.pipeline = make_pipeline("failed")
        watch.platform.head_sha = "newer"

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert await _event() is None

    async def test_a_pipeline_judged_before_posts_nothing(self, watch):
        await _owned(last_pipeline_id=100)
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=None)

        assert await _event() is None
        assert watch.platform.reads == 1

    async def test_two_deliveries_of_one_pipeline_post_once(self, watch):
        await _owned()
        watch.platform.pipeline = make_pipeline("failed")

        await asyncio.gather(*(watch.make().aevaluate(ref=BRANCH, pipeline_id=100) for _ in range(2)))

        assert await InboxEvent.objects.acount() == 1

    async def test_an_unowned_or_closed_branch_posts_nothing(self, watch):
        await _owned(closed_at=timezone.now())
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        await watch.make().aevaluate(ref="other", pipeline_id=101)

        assert await _event() is None
        assert watch.platform.reads == 0

    @pytest.mark.parametrize("status", ["running", "pending"])
    async def test_a_pipeline_still_running_posts_nothing(self, watch, status):
        await _owned()
        watch.platform.pipeline = make_pipeline(status)

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert await _event() is None
        assert (await MergeRequestOwnership.objects.aget()).last_pipeline_id is None

    async def test_a_failed_post_forgets_the_pipeline_so_it_is_judged_again(self, watch):
        await _owned(last_pipeline_id=99)
        watch.platform.pipeline = make_pipeline("failed")

        with (
            patch("sessions.pipeline_watch.service.apost_event", new=AsyncMock(side_effect=RuntimeError("db"))),
            pytest.raises(RuntimeError),
        ):
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert (await MergeRequestOwnership.objects.aget()).last_pipeline_id == 99
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        assert (await _event()).payload["pipeline_id"] == 100

    async def test_an_unreadable_pipeline_posts_nothing_and_is_retried_later(self, watch, caplog):
        await _owned()
        watch.platform.read_error = RuntimeError("boom")

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert await _event() is None
        assert (await MergeRequestOwnership.objects.aget()).last_pipeline_id is None


class TestGivingUp:
    async def test_spent_attempts_on_a_new_daiv_commit_tell_the_owner(self, watch):
        ownership = await _owned(attempts=3)
        watch.platform.pipeline = make_pipeline("failed", [make_job("failed", name="tests")])

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        [sent] = watch.exhausted
        assert sent["ownership"].pk == ownership.pk
        assert sent["session"].pk == ownership.session_id
        assert (sent["failing_jobs"], sent["sha"]) == (["tests"], SHA)

    async def test_a_retried_pipeline_on_the_same_commit_tells_the_owner_once(self, watch):
        await _owned(attempts=3)

        for pipeline_id in (100, 101):
            watch.platform.pipeline = make_pipeline("failed", pipeline_id=pipeline_id)
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=pipeline_id)

        assert len(watch.exhausted) == 1
        assert await InboxEvent.objects.filter(reason=QuietReason.BUDGET_SPENT).acount() == 2

    @pytest.mark.parametrize(
        "fields", [{"last_daiv_sha": "0a1b2c3d"}, {"last_examined_sha": SHA}], ids=["not-daivs", "examined"]
    )
    async def test_a_quiet_failure_for_another_reason_tells_no_one(self, watch, fields):
        await _owned(attempts=3, **fields)
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert watch.exhausted == []


class TestDelegatedOutcome:
    async def _child(self, **fields) -> tuple[Session, MergeRequestOwnership]:
        parent = await Session.objects.acreate(
            thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id="group/coordinator"
        )
        return parent, await _owned(session={"parent": parent}, **fields)

    @pytest.mark.parametrize(
        ("pipeline", "fields", "outcome"),
        [
            (make_pipeline("success"), {}, "passing"),
            (make_pipeline("failed"), {"attempts": 3}, "stopped_after_attempts"),
        ],
        ids=["passed", "budget-spent"],
    )
    async def test_the_parent_of_a_delegated_session_remembers_how_its_ci_ended(self, watch, pipeline, fields, outcome):
        parent, ownership = await self._child(**fields)
        watch.platform.pipeline = pipeline

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await InboxEvent.objects.aget(session=parent)
        assert (event.kind, event.wake, event.source_key) == (
            EventKind.DELEGATED_CI_OUTCOME, False, f"child-ci:{ownership.session_id}:{pipeline.id}",
        )  # fmt: skip
        assert event.payload == {
            "child_thread_id": ownership.session_id,
            "merge_request_iid": 7,
            "outcome": outcome,
            "web_url": pipeline.web_url,
        }

    @pytest.mark.parametrize("status", ["failed", "manual"], ids=["woken", "stopped"])
    async def test_other_verdicts_tell_the_parent_nothing(self, watch, status):
        await self._child()
        watch.platform.pipeline = make_pipeline(status)

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert not await InboxEvent.objects.filter(kind=EventKind.DELEGATED_CI_OUTCOME).aexists()

    async def test_a_session_without_a_parent_tells_no_one(self, watch):
        await _owned(attempts=3)
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert not await InboxEvent.objects.filter(kind=EventKind.DELEGATED_CI_OUTCOME).aexists()


class TestRequestEvaluation:
    async def test_it_enqueues_only_for_an_owned_branch(self, watch):
        await _owned()

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            assert await watch.make().arequest_evaluation(ref=BRANCH, pipeline_id=100) is True
            assert await watch.make().arequest_evaluation(ref="other", pipeline_id=101) is False

        task.aenqueue.assert_awaited_once_with(repo_id=REPO, ref=BRANCH, pipeline_id=100)


class TestToggle:
    async def test_turning_it_on_gives_the_attempts_back_and_judges_the_last_pipeline_again(self, watch):
        ownership = await _owned(watch_enabled=False, attempts=3, last_examined_sha="abc", last_pipeline_id=7)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            assert await watch.make().aturn_on(ownership) is True

        await ownership.arefresh_from_db()
        assert (ownership.watch_enabled, ownership.attempts, ownership.last_pipeline_id) == (True, 0, None)
        assert ownership.last_examined_sha == "abc"
        task.aenqueue.assert_awaited_once_with(repo_id=REPO, ref=BRANCH, pipeline_id=7)

    async def test_turning_it_on_when_it_is_on_changes_nothing(self, watch):
        ownership = await _owned(attempts=1, last_examined_sha=SHA, last_pipeline_id=100)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            assert await watch.make().aturn_on(ownership) is True

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_pipeline_id) == (1, 100)
        task.aenqueue.assert_not_awaited()

    @pytest.mark.parametrize("pipeline_id", [100, None], ids=["by-id", "latest"])
    async def test_a_failure_reported_while_the_watch_was_off_wakes_any_later_evaluation(
        self, watch, drain, pipeline_id
    ):
        ownership = await _owned(watch_enabled=False)
        watch.platform.pipeline = make_pipeline("failed")
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            await watch.make().aturn_on(ownership)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=pipeline_id)

        quiet, woken = [event async for event in InboxEvent.objects.order_by("created_at")]
        assert (quiet.wake, quiet.reason) == (False, QuietReason.WATCH_OFF)
        assert (woken.wake, woken.source_key) == (True, "pipeline:100")
        drain.assert_called_once_with(ownership.session_id)

    async def test_turning_it_back_on_during_a_fix_leaves_the_examined_commit_alone(self, watch):
        ownership = await _owned(watch_enabled=False, attempts=1, last_examined_sha=SHA)
        watch.platform.pipeline = make_pipeline("failed")

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            await watch.make().aturn_on(ownership)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.wake, event.reason) == (False, QuietReason.ALREADY_EXAMINED)

    async def test_it_cannot_be_turned_on_when_the_repository_has_it_off(self, watch):
        ownership = await _owned(watch_enabled=False)
        watch.policy.enabled = False

        assert await watch.make().aturn_on(ownership) is False
        await ownership.arefresh_from_db()
        assert ownership.watch_enabled is False

    async def test_turning_it_off_keeps_the_attempts(self, watch):
        ownership = await _owned(attempts=2)

        await watch.make().aturn_off(ownership)

        await ownership.arefresh_from_db()
        assert (ownership.watch_enabled, ownership.attempts) == (False, 2)


async def test_a_deleted_owning_session_is_replaced_before_posting(watch):
    ownership = await _owned()
    await Session.objects.filter(pk=ownership.session_id).adelete()
    watch.platform.pipeline = make_pipeline("success")

    await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

    event = await _event()
    await ownership.arefresh_from_db()
    assert event.session_id == ownership.session_id is not None
