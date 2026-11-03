import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.inbox import kinds
from sessions.models import EventKind, InboxEvent, MergeRequestOwnership, Session, SessionOrigin
from sessions.pipeline_watch.reconciler import WatchReconciler
from sessions.pipeline_watch.service import PipelineWatch

from ..conftest import make_pipeline

pytestmark = pytest.mark.django_db(transaction=True)


async def _owned(iid: int, *, repo_id: str = "group/repo", branch: str = "daiv/branch", age=timedelta(hours=1)):
    session = await Session.objects.acreate(
        thread_id=str(uuid.uuid4()), origin=SessionOrigin.CHAT, repo_id=repo_id, ref=branch
    )
    ownership = await MergeRequestOwnership.objects.acreate(
        repo_id=repo_id, merge_request_iid=iid, session=session, source_branch=branch, last_daiv_sha="abc"
    )
    await MergeRequestOwnership.objects.filter(pk=ownership.pk).aupdate(updated_at=timezone.now() - age)
    return ownership


class RecordingWatch:
    evaluated: list[tuple] = []
    failing: set[str] = set()
    unread: set[str] = set()

    def __init__(self, repo_id):
        self.repo_id = repo_id

    async def aevaluate_latest(self, *, ref):
        RecordingWatch.evaluated.append((self.repo_id, ref))
        if self.repo_id in RecordingWatch.failing:
            raise RuntimeError("platform down")
        return self.repo_id not in RecordingWatch.unread


@pytest.fixture(autouse=True)
def _reset_watch():
    RecordingWatch.evaluated, RecordingWatch.failing, RecordingWatch.unread = [], set(), set()


async def _sweep() -> int:
    return await WatchReconciler(watch_factory=RecordingWatch).areconcile()


async def test_it_re_reads_the_latest_pipelines_of_branches_with_no_ci_result():
    await _owned(7)
    await _owned(8, branch="fresh", age=timedelta(minutes=5))

    assert await _sweep() == 1
    assert RecordingWatch.evaluated == [("group/repo", "daiv/branch")]


async def test_nothing_expires_and_no_note_is_posted():
    ownership = await _owned(7, age=timedelta(days=3))

    with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note") as note:
        assert await _sweep() == 1

    note.assert_not_called()
    await ownership.arefresh_from_db()
    assert (ownership.closed_at, ownership.watch_enabled) == (None, True)


async def test_one_failing_branch_does_not_stop_the_sweep(caplog):
    await _owned(7, repo_id="group/broken")
    await _owned(8, repo_id="group/fine")
    RecordingWatch.failing = {"group/broken"}

    with caplog.at_level("ERROR", logger="daiv.sessions"):
        touched = await _sweep()

    assert touched == 2
    assert {repo for repo, _ in RecordingWatch.evaluated} == {"group/broken", "group/fine"}
    assert "reconcile failed for group/broken@daiv/branch" in caplog.text


async def test_a_sweep_is_bounded_and_the_next_one_reads_the_branches_it_left_out():
    for iid, age in ((1, timedelta(hours=1)), (2, timedelta(hours=3)), (3, timedelta(hours=2))):
        await _owned(iid, branch=f"b{iid}", age=age)

    with patch("sessions.pipeline_watch.reconciler.WATCH_SWEEP_LIMIT", 2):
        first = (await _sweep(), [ref for _, ref in RecordingWatch.evaluated])
        RecordingWatch.evaluated = []
        second = (await _sweep(), [ref for _, ref in RecordingWatch.evaluated])

    assert first == (2, ["b2", "b3"])
    assert second == (1, ["b1"])


async def test_a_branch_read_recently_waits_its_turn_until_it_is_quiet_again():
    await _owned(7)
    await _sweep()
    RecordingWatch.evaluated = []

    assert await _sweep() == 0

    later = timezone.now() + timedelta(minutes=31)
    with patch("sessions.pipeline_watch.reconciler.timezone.now", return_value=later):
        assert await _sweep() == 1


@pytest.mark.parametrize(
    ("unread", "failing", "next_tick"),
    [(set(), set(), 0), ({"group/repo"}, set(), 1), (set(), {"group/repo"}, 1)],
    ids=["read", "unread", "raised"],
)
async def test_a_branch_not_read_in_full_is_read_again_on_the_next_tick(unread, failing, next_tick):
    await _owned(7)
    RecordingWatch.unread, RecordingWatch.failing = unread, failing
    await _sweep()

    later = timezone.now() + timedelta(minutes=10)
    with patch("sessions.pipeline_watch.reconciler.timezone.now", return_value=later):
        assert await _sweep() == next_tick


class FakePlatform:
    def __init__(self, *pipelines):
        self.pipelines = list(pipelines)

    async def aensure_client(self):
        return None

    async def aread_latest_pipelines(self, *, ref):
        return [pipeline.model_copy(update={"jobs": []}) for pipeline in self.pipelines]

    async def aread_pipeline(self, *, pipeline_id):
        return next(pipeline for pipeline in self.pipelines if pipeline.id == pipeline_id)

    async def ais_head_pipeline(self, *, merge_request_iid, report):
        return report.sha == self.pipelines[0].sha


def _watch_factory(platform):
    policy = SimpleNamespace(enabled=True, max_attempts=3)
    return lambda repo_id: PipelineWatch(repo_id, platform=platform, policy=policy)


async def test_a_manual_retry_that_passed_without_a_webhook_is_remembered_as_passing():
    """A retried pipeline keeps its id: the sweep reads its new status although the commit was judged before."""
    ownership = await _owned(7, age=timedelta(hours=2))
    failed = make_pipeline("failed")
    session = await Session.objects.aget(pk=ownership.session_id)
    payload = {"pipeline_id": failed.id, "sha": failed.sha, "status": "failed", "web_url": failed.web_url}
    await InboxEvent.objects.acreate(
        session=session, kind=EventKind.CI_FAILED, source_key=kinds.SourceKey.pipeline(failed.id), subject="ci:7",
        merge_request_iid=7, wake=False, reason="already_examined", created_at=timezone.now() - timedelta(hours=1),
        payload=payload | {"failed_jobs": []},
    )  # fmt: skip
    await MergeRequestOwnership.objects.filter(pk=ownership.pk).aupdate(
        last_daiv_sha=failed.sha, last_pipeline_id=failed.id, last_pipeline_status="failed"
    )
    watch = _watch_factory(FakePlatform(make_pipeline("success")))

    with patch("sessions.inbox.post.drain_logged"):
        assert await WatchReconciler(watch_factory=watch).areconcile() == 1
        assert await WatchReconciler(watch_factory=watch).areconcile() == 0

    latest = await InboxEvent.objects.order_by("-created_at").afirst()
    assert (latest.kind, latest.source_key) == (EventKind.CI_PASSED, "pipeline:100:ci_passed")


async def test_a_github_workflow_failure_missed_behind_another_workflows_run_is_remembered():
    """GitHub runs one workflow run per workflow on a commit, so the newest run on the branch hides another's."""
    await _owned(7, age=timedelta(hours=2))
    lint = make_pipeline("failed").model_copy(update={"workflow_id": 11, "workflow_name": "Lint"})
    tests = make_pipeline("success", pipeline_id=101).model_copy(update={"workflow_id": 12, "workflow_name": "Tests"})

    with patch("sessions.inbox.post.drain_logged"):
        assert await WatchReconciler(watch_factory=_watch_factory(FakePlatform(tests, lint))).areconcile() == 1

    assert {(event.kind, event.subject) async for event in InboxEvent.objects.all()} == {
        (EventKind.CI_PASSED, "ci:7:12"),
        (EventKind.CI_FAILED, "ci:7:11"),
    }
