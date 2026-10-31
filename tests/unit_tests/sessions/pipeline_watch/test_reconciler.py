import uuid
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

import pytest
from sessions.models import MergeRequestOwnership, Session, SessionOrigin
from sessions.pipeline_watch.reconciler import WatchReconciler

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

    def __init__(self, repo_id):
        self.repo_id = repo_id

    async def aevaluate(self, *, ref, pipeline_id):
        RecordingWatch.evaluated.append((self.repo_id, ref, pipeline_id))
        if self.repo_id in RecordingWatch.failing:
            raise RuntimeError("platform down")


@pytest.fixture(autouse=True)
def _reset_watch():
    RecordingWatch.evaluated, RecordingWatch.failing = [], set()


async def test_it_re_reads_the_latest_pipeline_of_branches_with_no_ci_result():
    await _owned(7)
    await _owned(8, branch="fresh", age=timedelta(minutes=5))

    touched = await WatchReconciler(watch_factory=RecordingWatch).areconcile()

    assert touched == 1
    assert RecordingWatch.evaluated == [("group/repo", "daiv/branch", None)]


async def test_nothing_expires_and_no_note_is_posted():
    ownership = await _owned(7, age=timedelta(days=3))

    with patch("sessions.pipeline_watch.platform.WatchPlatform.apost_note") as note:
        assert await WatchReconciler(watch_factory=RecordingWatch).areconcile() == 0

    note.assert_not_called()
    await ownership.arefresh_from_db()
    assert (ownership.closed_at, ownership.watch_enabled) == (None, True)


async def test_one_failing_branch_does_not_stop_the_sweep(caplog):
    await _owned(7, repo_id="group/broken")
    await _owned(8, repo_id="group/fine")
    RecordingWatch.failing = {"group/broken"}

    with caplog.at_level("ERROR", logger="daiv.sessions"):
        touched = await WatchReconciler(watch_factory=RecordingWatch).areconcile()

    assert touched == 2
    assert {repo for repo, _, _ in RecordingWatch.evaluated} == {"group/broken", "group/fine"}
    assert "reconcile failed for group/broken@daiv/branch" in caplog.text


async def test_a_sweep_is_bounded():
    for iid in range(3):
        await _owned(iid + 1, branch=f"b{iid}")

    with patch("sessions.pipeline_watch.reconciler.WATCH_SWEEP_LIMIT", 2):
        assert await WatchReconciler(watch_factory=RecordingWatch).areconcile() == 2
