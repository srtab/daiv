"""The shared post-run arm: what every publishing seam funnels through.

The seam wiring itself (which callers reach this) is pinned per caller — see
``tests/unit_tests/jobs/test_run_job_task_arms_watch.py``,
``tests/unit_tests/codebase/managers/test_issue_addressor_arms_watch.py`` and
``tests/unit_tests/chat/test_chat_arms_watch.py``.
"""

from types import SimpleNamespace

import pytest
from sessions.pipeline_watch.service import PipelineWatch
from sessions.pipeline_watch.store import WatchStore

from tests.unit_tests.test_template_comments import DAIV_DIR

MR = {"merge_request_id": 7, "source_branch": "daiv/branch"}

# Every seam that finishes a publishing agent run, and whether it arms the watch. Discovered
# from the source below rather than trusted, because a new seam that forgets to arm is
# invisible: the feature just silently stops covering the merge requests it produces.
ARMING_SEAMS = {"jobs/tasks.py", "codebase/managers/issue_addressor.py", "chat/api/streaming.py"}


class RecordingWatch(PipelineWatch):
    """Records the arm/exhaust decisions ``aarm_after_run`` makes, without touching the platform."""

    def __init__(self, repo_id: str = "group/repo", **kwargs):
        super().__init__(repo_id, **kwargs)
        self.armed = []
        self.exhausted = []

    async def aarm(self, **kwargs):
        self.armed.append(kwargs)
        return "mr-thread"

    async def aexhaust(self, **kwargs):
        self.exhausted.append(kwargs)


class FixRunStore(WatchStore):
    """Answers the ``trigger_type`` read without a Run row."""

    def __init__(self, verdict: bool):
        self._verdict = verdict

    async def ais_fix_run(self, run_id):
        return self._verdict


@pytest.fixture
def stub_watch(monkeypatch):
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeTask())
    return SimpleNamespace(watch=RecordingWatch(), enqueued=enqueued)


@pytest.mark.django_db
async def test_a_published_run_arms_the_watch(stub_watch):
    await stub_watch.watch.aarm_after_run(run_id=None, merge_request=MR, published=True)

    assert stub_watch.watch.armed[0]["merge_request_iid"] == 7
    assert stub_watch.watch.armed[0]["ref"] == "daiv/branch"
    assert stub_watch.watch.armed[0]["was_fix_run"] is False
    # Arming must schedule an immediate evaluation, or the event our own push
    # triggered is missed and the watch waits for the reconciler.
    assert stub_watch.enqueued[0]["ref"] == "daiv/branch"


@pytest.mark.django_db
async def test_a_run_without_a_merge_request_arms_nothing(stub_watch):
    await stub_watch.watch.aarm_after_run(run_id=None, merge_request=None, published=True)
    assert stub_watch.watch.armed == []


@pytest.mark.django_db
async def test_a_fix_run_arms_without_resetting_the_counter(stub_watch):
    watch = RecordingWatch(store=FixRunStore(True))

    await watch.aarm_after_run(run_id="a-fix-run", merge_request=MR, published=True)

    assert watch.armed[0]["was_fix_run"] is True


@pytest.mark.django_db
async def test_a_fix_run_that_pushed_nothing_ends_the_watch(stub_watch):
    """Don't re-arm a watch nothing will ever move: no push means no pipeline, no event."""
    watch = RecordingWatch(store=FixRunStore(True))

    await watch.aarm_after_run(run_id="a-fix-run", merge_request=MR, published=False)

    assert watch.armed == []
    assert watch.exhausted[0]["merge_request_iid"] == 7


@pytest.mark.django_db
async def test_a_no_op_turn_on_an_existing_mr_does_not_re_arm(stub_watch):
    """A turn that changed nothing still carries the MR it sits on, so ``merge_request`` alone
    cannot gate the arm — re-arming here would reset the budget and re-judge an old pipeline,
    kicking off a fix run off the back of a turn that pushed nothing."""
    await stub_watch.watch.aarm_after_run(run_id=None, merge_request=MR, published=False)

    assert stub_watch.watch.armed == []
    assert stub_watch.watch.exhausted == []
    assert stub_watch.enqueued == []


@pytest.mark.django_db
async def test_the_fix_run_verdict_comes_from_the_run_row(stub_watch, django_user_model):
    """``WatchStore.ais_fix_run`` is the real read here rather than a stub: a dispatcher that
    forgets to thread its ``run_id`` through silently resets the attempt counter and unbounds the loop."""
    from sessions.models import Run, RunStatus, Session, SessionOrigin

    session = await Session.objects.acreate(
        thread_id="mr-thread", origin=SessionOrigin.MR_WEBHOOK, repo_id="group/repo", ref="daiv/branch"
    )
    run = await Run.objects.acreate(
        session=session, trigger_type=SessionOrigin.PIPELINE_WEBHOOK, repo_id="group/repo", status=RunStatus.SUCCESSFUL
    )

    await stub_watch.watch.aarm_after_run(run_id=str(run.pk), merge_request=MR, published=True)

    assert stub_watch.watch.armed[0]["was_fix_run"] is True


def test_every_publishing_seam_that_should_arm_does():
    """The review addressor is absent on purpose: it pushes to a merge request someone else may
    own, so babysitting its pipeline would put unrequested commits on their branch."""
    callers = {
        path.relative_to(DAIV_DIR).as_posix()
        for path in DAIV_DIR.rglob("*.py")
        if ".aarm_after_run(" in path.read_text()
    }

    assert callers == ARMING_SEAMS
