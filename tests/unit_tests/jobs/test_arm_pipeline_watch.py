import pytest


@pytest.mark.django_db
async def test_a_successful_run_with_a_merge_request_arms_the_watch(monkeypatch):
    from jobs.tasks import _aarm_watch_after_run

    armed = []

    async def fake_arm(**kwargs):
        armed.append(kwargs)
        return "mr-thread"

    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("jobs.tasks.aarm_watch", fake_arm)
    monkeypatch.setattr("jobs.tasks.evaluate_pipeline_watch_task", FakeTask())

    await _aarm_watch_after_run(
        repo_id="group/repo",
        trigger_type="api_job",
        merge_request={"merge_request_id": 7, "source_branch": "daiv/branch"},
        code_changes=True,
    )

    assert armed[0]["merge_request_iid"] == 7
    assert armed[0]["ref"] == "daiv/branch"
    assert armed[0]["was_fix_run"] is False
    # Arming must schedule an immediate evaluation, or the event our own push
    # triggered is missed and the watch waits for the reconciler.
    assert enqueued[0]["ref"] == "daiv/branch"


@pytest.mark.django_db
async def test_a_run_without_a_merge_request_arms_nothing(monkeypatch):
    from jobs.tasks import _aarm_watch_after_run

    armed = []

    async def fake_arm(**kwargs):
        armed.append(kwargs)
        return None

    monkeypatch.setattr("jobs.tasks.aarm_watch", fake_arm)
    await _aarm_watch_after_run(repo_id="group/repo", trigger_type="api_job", merge_request=None, code_changes=True)
    assert armed == []


@pytest.mark.django_db
async def test_a_fix_run_arms_without_resetting_the_counter(monkeypatch):
    from jobs.tasks import _aarm_watch_after_run

    armed = []

    async def fake_arm(**kwargs):
        armed.append(kwargs)
        return "mr-thread"

    class FakeTask:
        async def aenqueue(self, **kwargs):
            pass

    monkeypatch.setattr("jobs.tasks.aarm_watch", fake_arm)
    monkeypatch.setattr("jobs.tasks.evaluate_pipeline_watch_task", FakeTask())

    await _aarm_watch_after_run(
        repo_id="group/repo",
        trigger_type="pipeline_webhook",
        merge_request={"merge_request_id": 7, "source_branch": "daiv/branch"},
        code_changes=True,
    )
    assert armed[0]["was_fix_run"] is True


@pytest.mark.django_db
async def test_a_fix_run_that_changed_nothing_ends_the_watch(monkeypatch):
    """Invariant 7 at the wiring level: don't re-arm a watch nothing will ever move."""
    from jobs.tasks import _aarm_watch_after_run

    armed, exhausted = [], []

    async def fake_arm(**kwargs):
        armed.append(kwargs)
        return "mr-thread"

    async def fake_exhaust(**kwargs):
        exhausted.append(kwargs)

    monkeypatch.setattr("jobs.tasks.aarm_watch", fake_arm)
    monkeypatch.setattr("jobs.tasks.aexhaust_watch", fake_exhaust)

    await _aarm_watch_after_run(
        repo_id="group/repo",
        trigger_type="pipeline_webhook",
        merge_request={"merge_request_id": 7, "source_branch": "daiv/branch"},
        code_changes=False,
    )

    assert armed == []
    assert exhausted[0]["merge_request_iid"] == 7


@pytest.mark.django_db
async def test_a_first_run_that_changed_nothing_arms_nothing(monkeypatch):
    """A non-fix run with no diff published no MR, so there is nothing to watch and
    nothing to exhaust."""
    from jobs.tasks import _aarm_watch_after_run

    armed, exhausted = [], []

    async def fake_arm(**kwargs):
        armed.append(kwargs)
        return "mr-thread"

    async def fake_exhaust(**kwargs):
        exhausted.append(kwargs)

    monkeypatch.setattr("jobs.tasks.aarm_watch", fake_arm)
    monkeypatch.setattr("jobs.tasks.aexhaust_watch", fake_exhaust)

    await _aarm_watch_after_run(
        repo_id="group/repo",
        trigger_type="api_job",
        merge_request={"merge_request_id": 7, "source_branch": "daiv/branch"},
        code_changes=False,
    )

    assert armed == []
    assert exhausted == []
