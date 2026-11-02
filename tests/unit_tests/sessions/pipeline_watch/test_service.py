import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.utils import timezone

import pytest
from sessions.inbox.drain import newest_of_each_subject
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
from sessions.pipeline_watch.store import WatchStore
from sessions.signal_defs import watch_exhausted

from ..conftest import make_job, make_pipeline

pytestmark = pytest.mark.django_db(transaction=True)

REPO, BRANCH, SHA = "group/repo", "daiv/branch", "abc123"


class FakePlatform:
    """Stands in for the git platform. ``head_sha`` is what the correlation compares against."""

    def __init__(self):
        self.pipeline = None
        self.latest = []
        self.head_sha = SHA
        self.read_error = None
        self.reads = 0

    async def aensure_client(self):
        return None

    async def aread_pipeline(self, *, pipeline_id):
        # A real yield point, so concurrent evaluations both get past the read before either records the pipeline.
        await asyncio.sleep(0)
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return next((pipeline for pipeline in self.latest if pipeline.id == pipeline_id), self.pipeline)

    async def aread_latest_pipelines(self, *, ref):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return [pipeline.model_copy(update={"jobs": []}) for pipeline in self.latest]

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


async def _events() -> list[InboxEvent]:
    return [event async for event in InboxEvent.objects.order_by("created_at")]


def _workflow_run(status: str, *, pipeline_id: int = 100, workflow_id: int = 11, name: str = "Lint"):
    """A GitHub Actions run: one of the workflow runs on a commit."""
    return make_pipeline(status, pipeline_id=pipeline_id).model_copy(
        update={"workflow_id": workflow_id, "workflow_name": name}
    )


class TestVerdicts:
    async def test_a_green_pipeline_is_remembered_as_passing(self, watch):
        ownership = await _owned()
        watch.platform.pipeline = make_pipeline("success")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.session_id, event.kind, event.wake) == (ownership.session_id, EventKind.CI_PASSED, False)
        assert (event.source_key, event.subject, event.ref, event.merge_request_iid) == (
            "pipeline:100:ci_passed",
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


class TestCiSubjects:
    """A GitLab pipeline covers the whole commit, so the merge request has one CI subject; GitHub runs one workflow
    run per workflow on a commit, and each workflow is a subject of its own."""

    async def test_a_gitlab_pipeline_is_the_merge_requests_one_subject(self, watch):
        await _owned()
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert event.subject == "ci:7"
        assert "workflow" not in event.payload

    async def test_a_github_workflow_run_is_a_subject_of_its_own_named_for_its_workflow(self, watch):
        await _owned()
        watch.platform.pipeline = _workflow_run("failed", workflow_id=11, name="Lint")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.subject, event.payload["workflow"]) == ("ci:7:11", "Lint")

    async def test_one_workflows_pass_does_not_replace_anothers_waking_failure(self, watch):
        await _owned()
        watch.platform.pipeline = _workflow_run("failed", pipeline_id=100, workflow_id=11)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        watch.platform.pipeline = _workflow_run("success", pipeline_id=101, workflow_id=12, name="Tests")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)

        failed, passed = await _events()
        assert (failed.kind, failed.wake, passed.kind) == (EventKind.CI_FAILED, True, EventKind.CI_PASSED)
        assert newest_of_each_subject([failed, passed]) == [failed, passed]

    async def test_a_later_gitlab_pipeline_replaces_the_earlier_one(self, watch):
        await _owned()
        for pipeline_id, status in ((100, "failed"), (101, "success")):
            watch.platform.pipeline = make_pipeline(status, pipeline_id=pipeline_id)
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=pipeline_id)

        failed, passed = await _events()
        assert newest_of_each_subject([failed, passed]) == [passed]


class TestLatestOfEachSubject:
    """What the reconciler judges on a branch nothing was heard of: the latest pipeline of each CI subject, since on
    GitHub the newest run on a branch is one workflow's of several."""

    async def test_a_github_workflow_failure_is_judged_though_another_workflow_ran_after_it(self, watch):
        await _owned()
        watch.platform.latest = [
            _workflow_run("success", pipeline_id=101, workflow_id=12, name="Tests"),
            _workflow_run("failed", pipeline_id=100, workflow_id=11),
        ]

        await watch.make().aevaluate_latest(ref=BRANCH)

        assert [(event.kind, event.subject, event.wake) for event in await _events()] == [
            (EventKind.CI_PASSED, "ci:7:12", False),
            (EventKind.CI_FAILED, "ci:7:11", True),
        ]

    async def test_a_gitlab_branch_has_its_one_latest_pipeline_judged(self, watch):
        await _owned()
        watch.platform.latest = [make_pipeline("failed")]

        await watch.make().aevaluate_latest(ref=BRANCH)

        event = await _event()
        assert (event.kind, event.subject, event.wake) == (EventKind.CI_FAILED, "ci:7", True)

    async def test_a_pipeline_already_judged_in_its_status_is_not_judged_again(self, watch):
        """The merge request remembers only the last pipeline judged, so another workflow's failure the drain has
        examined since would otherwise be posted again as already examined."""
        await _owned()
        watch.platform.latest = [
            _workflow_run("failed", pipeline_id=100, workflow_id=11),
            _workflow_run("success", pipeline_id=101, workflow_id=12, name="Tests"),
        ]
        await watch.make().aevaluate_latest(ref=BRANCH)
        await MergeRequestOwnership.objects.aupdate(last_examined_sha=SHA)
        watch.platform.reads = 0

        await watch.make().aevaluate_latest(ref=BRANCH)

        assert [event.payload["pipeline_id"] for event in await _events()] == [100, 101]
        assert watch.platform.reads == 1

    async def test_a_watch_turned_back_on_judges_them_anew(self, watch):
        """Turning the watch on forgets the last pipeline judged, so a failure it kept quiet wakes the session."""
        ownership = await _owned(watch_enabled=False)
        watch.platform.latest = [_workflow_run("failed", pipeline_id=100, workflow_id=11)]
        await watch.make().aevaluate_latest(ref=BRANCH)
        await WatchStore().aset_enabled(ownership.pk, enabled=True)

        await watch.make().aevaluate_latest(ref=BRANCH)

        assert [(event.reason, event.wake) for event in await _events()] == [(QuietReason.WATCH_OFF, False), ("", True)]

    async def test_a_failure_kept_quiet_while_the_watch_was_off_is_judged_again_once_it_is_on(self, watch):
        """Another workflow judged since keeps the merge request's last pipeline set, so only the quiet reason tells
        that the failure was never judged with the watch on."""
        ownership = await _owned(watch_enabled=False)
        watch.platform.latest = [
            _workflow_run("failed", pipeline_id=100, workflow_id=11),
            _workflow_run("success", pipeline_id=101, workflow_id=12, name="Tests"),
        ]
        await watch.make().aevaluate_latest(ref=BRANCH)
        await MergeRequestOwnership.objects.filter(pk=ownership.pk).aupdate(watch_enabled=True)

        await watch.make().aevaluate_latest(ref=BRANCH)

        assert [(event.subject, event.reason, event.wake) for event in await _events()] == [
            ("ci:7:11", QuietReason.WATCH_OFF, False),
            ("ci:7:12", "", False),
            ("ci:7:11", "", True),
        ]

    async def test_an_unreadable_branch_posts_nothing_and_says_it_was_not_read(self, watch, caplog):
        await _owned()
        watch.platform.read_error = RuntimeError("boom")

        with caplog.at_level("ERROR", logger="daiv.sessions"):
            assert await watch.make().aevaluate_latest(ref=BRANCH) is False

        assert await _event() is None
        assert "the latest pipelines" in caplog.text

    async def test_a_subject_that_fails_to_be_judged_does_not_stop_the_others(self, watch):
        await _owned()
        watch.platform.latest = [
            _workflow_run("failed", pipeline_id=100, workflow_id=11),
            _workflow_run("success", pipeline_id=101, workflow_id=12, name="Tests"),
        ]
        real_post = PipelineWatch._apost

        async def post(self, *, ownership, session, report):
            if report.id == 100:
                raise RuntimeError("post failed")
            await real_post(self, ownership=ownership, session=session, report=report)

        with patch.object(PipelineWatch, "_apost", post), pytest.raises(RuntimeError, match="post failed"):
            await watch.make().aevaluate_latest(ref=BRANCH)

        assert [event.payload["pipeline_id"] for event in await _events()] == [101]


class TestOutOfOrder:
    """A pipeline older than one already judged on the same commit and CI subject is replaced: reported late, it
    neither wakes a turn nor turns a pass back into a failure."""

    async def test_a_late_gitlab_pipeline_on_the_same_commit_is_skipped(self, watch, drain):
        await _owned()
        watch.platform.pipeline = make_pipeline("success", pipeline_id=101)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)
        watch.platform.pipeline = make_pipeline("failed", pipeline_id=100)

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert [event.kind for event in await _events()] == [EventKind.CI_PASSED]
        drain.assert_not_called()

    async def test_a_late_github_run_is_skipped_only_within_its_own_workflow(self, watch):
        await _owned()
        watch.platform.pipeline = _workflow_run("success", pipeline_id=101, workflow_id=11)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)

        late_runs = (
            _workflow_run("failed", pipeline_id=100, workflow_id=11),
            _workflow_run("failed", pipeline_id=99, workflow_id=12),
        )
        for late in late_runs:
            watch.platform.pipeline = late
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=late.id)

        assert [(event.kind, event.payload["pipeline_id"]) for event in await _events()] == [
            (EventKind.CI_PASSED, 101),
            (EventKind.CI_FAILED, 99),
        ]

    async def test_a_newer_pipeline_on_another_commit_does_not_hide_one_on_the_head(self, watch):
        await _owned(last_daiv_sha="")
        watch.platform.head_sha = "0a1b2c3d"
        watch.platform.pipeline = make_pipeline("success", pipeline_id=101).model_copy(update={"sha": "0a1b2c3d"})
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)
        watch.platform.head_sha = SHA
        watch.platform.pipeline = make_pipeline("failed", pipeline_id=100)

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert [event.payload["pipeline_id"] for event in await _events()] == [101, 100]


class TestWakeRule:
    async def test_the_watch_turned_off_for_the_merge_request(self, watch):
        await _owned(watch_enabled=False)

        assert await _quiet_reason(watch) == QuietReason.WATCH_OFF

    async def test_the_watch_turned_off_for_the_repository(self, watch):
        await _owned()
        watch.policy.enabled = False

        assert await _quiet_reason(watch) == QuietReason.WATCH_OFF

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
        await _owned(last_pipeline_id=100, last_pipeline_status="failed")
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        watch.platform.latest = [make_pipeline("failed")]
        await watch.make().aevaluate_latest(ref=BRANCH)

        assert await _event() is None

    async def test_a_retried_pipeline_that_passes_is_remembered_as_passing(self, watch):
        """A retry keeps the pipeline's id; its new status is judged and posted under a key of its own."""
        ownership = await _owned()
        watch.platform.pipeline = make_pipeline("failed")
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        watch.platform.pipeline = make_pipeline("success")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        events = [event async for event in InboxEvent.objects.filter(session=ownership.session).order_by("created_at")]
        assert [(event.kind, event.source_key) for event in events] == [
            (EventKind.CI_FAILED, "pipeline:100"),
            (EventKind.CI_PASSED, "pipeline:100:ci_passed"),
        ]

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

    async def test_a_give_up_records_how_many_attempts_were_spent(self, watch):
        await _owned(attempts=3)
        watch.platform.pipeline = make_pipeline("failed", [make_job("failed", name="tests")])

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await _event()
        assert (event.reason, event.payload["attempts"], event.payload["failed_jobs"]) == (
            QuietReason.BUDGET_SPENT, 3, ["tests"],
        )  # fmt: skip

    async def test_a_failure_kept_quiet_for_another_reason_records_no_attempts(self, watch):
        await _owned(attempts=3, last_examined_sha=SHA)
        watch.platform.pipeline = make_pipeline("failed")

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert "attempts" not in (await _event()).payload

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
            EventKind.DELEGATED_CI_OUTCOME, False, f"child-ci:{ownership.session_id}:{pipeline.id}:{outcome}",
        )  # fmt: skip
        assert event.payload == {
            "child_thread_id": ownership.session_id,
            "merge_request_iid": 7,
            "outcome": outcome,
            "web_url": pipeline.web_url,
        }

    async def test_one_workflows_pass_is_no_pass_while_another_workflow_fails(self, watch):
        parent, _ = await self._child()
        lint_failed = _workflow_run("failed", pipeline_id=100, workflow_id=11)
        tests_passed = _workflow_run("success", pipeline_id=101, workflow_id=12)
        watch.platform.latest = [tests_passed, lint_failed]
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)

        assert not await InboxEvent.objects.filter(session=parent).aexists()

        watch.platform.latest = [tests_passed, _workflow_run("success", pipeline_id=100, workflow_id=11)]
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        event = await InboxEvent.objects.aget(session=parent)
        assert (event.payload["outcome"], event.source_key.split(":")[-2]) == ("passing", "100")

    async def test_one_workflows_pass_is_no_pass_while_another_workflow_still_runs(self, watch):
        """A workflow still running has posted nothing, so only the platform knows it is there."""
        parent, _ = await self._child()
        tests_passed = _workflow_run("success", pipeline_id=101, workflow_id=12)
        watch.platform.latest = [tests_passed, _workflow_run("running", pipeline_id=100, workflow_id=11)]

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)

        assert not await InboxEvent.objects.filter(session=parent).aexists()

    async def test_a_workflow_that_succeeded_before_its_result_was_posted_counts_as_passing(self, watch):
        parent, _ = await self._child()
        watch.platform.latest = [
            _workflow_run("success", pipeline_id=101, workflow_id=12),
            _workflow_run("success", pipeline_id=100, workflow_id=11),
        ]

        await watch.make().aevaluate(ref=BRANCH, pipeline_id=101)

        assert await InboxEvent.objects.filter(session=parent, kind=EventKind.DELEGATED_CI_OUTCOME).aexists()

    async def test_unreadable_latest_pipelines_tell_the_parent_nothing(self, watch):
        parent, _ = await self._child()
        watch.platform.pipeline = make_pipeline("success")

        async def unreadable(*, ref):
            raise RuntimeError("boom")

        with patch.object(watch.platform, "aread_latest_pipelines", unreadable):
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        assert not await InboxEvent.objects.filter(session=parent).aexists()

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
    async def test_turning_it_on_gives_the_attempts_back_and_judges_the_latest_pipelines_again(self, watch):
        ownership = await _owned(watch_enabled=False, attempts=3, last_examined_sha="abc", last_pipeline_id=7)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            assert await watch.make().aturn_on(ownership) is True

        await ownership.arefresh_from_db()
        assert (ownership.watch_enabled, ownership.attempts, ownership.last_pipeline_id) == (True, 0, None)
        assert ownership.last_examined_sha == "abc"
        task.aenqueue.assert_awaited_once_with(repo_id=REPO, ref=BRANCH)

    async def test_turning_it_on_when_it_is_on_changes_nothing(self, watch):
        ownership = await _owned(attempts=1, last_examined_sha=SHA, last_pipeline_id=100)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            assert await watch.make().aturn_on(ownership) is True

        await ownership.arefresh_from_db()
        assert (ownership.attempts, ownership.last_pipeline_id) == (1, 100)
        task.aenqueue.assert_not_awaited()

    @pytest.mark.parametrize("latest", [False, True], ids=["by-id", "latest"])
    async def test_a_failure_reported_while_the_watch_was_off_wakes_any_later_evaluation(self, watch, drain, latest):
        ownership = await _owned(watch_enabled=False)
        watch.platform.pipeline = make_pipeline("failed")
        watch.platform.latest = [watch.platform.pipeline]
        await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

        with patch("sessions.tasks.evaluate_pipeline_watch_task") as task:
            task.aenqueue = AsyncMock()
            await watch.make().aturn_on(ownership)
        if latest:
            await watch.make().aevaluate_latest(ref=BRANCH)
        else:
            await watch.make().aevaluate(ref=BRANCH, pipeline_id=100)

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
