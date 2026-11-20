from unittest.mock import patch

import pytest
from sessions.models import EventKind, InboxEvent, QuietReason
from webhooks.github.callbacks import WorkflowRunCallback
from webhooks.github.models import Repository, WorkflowRun

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from tests.unit_tests.conftest import site_snapshot
from tests.unit_tests.sessions.conftest import amake_owned_merge_request


@pytest.fixture
def repo_config():
    return RepositoryConfig()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, repo_config):
    monkeypatch.setattr("webhooks.github.callbacks.RepositoryConfig.get_config", lambda *a, **kw: repo_config)


def make_callback(
    *, action: str = "completed", conclusion: str | None = "failure", branch: str = "daiv/branch"
) -> WorkflowRunCallback:
    return WorkflowRunCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        workflow_run=WorkflowRun(
            id=100, name="CI", status="completed", conclusion=conclusion, head_branch=branch, head_sha="abc123"
        ),
    )


def test_a_completed_run_is_accepted(monkeypatch_dependencies):
    assert make_callback().accept_callback() is True


def test_a_run_still_going_is_ignored(monkeypatch_dependencies):
    assert make_callback(action="requested", conclusion=None).accept_callback() is False


@pytest.mark.parametrize("switch", ["repository", "site"])
def test_a_run_is_accepted_with_the_watch_turned_off(monkeypatch_dependencies, repo_config, switch):
    """A watch turned off still remembers CI results, so whether one wakes the session is the evaluation's call."""
    repo_config.pipeline_watch.enabled = switch != "repository"
    site = site_snapshot(pipeline_watch_enabled=switch != "site")

    with patch.object(site_settings, "snapshot", return_value=site):
        assert make_callback().accept_callback() is True


@pytest.mark.django_db(transaction=True)
async def test_a_failure_with_the_watch_off_is_remembered(monkeypatch_dependencies, repo_config, evaluate_inline):
    repo_config.pipeline_watch.enabled = False
    ownership = await amake_owned_merge_request(repo_id="owner/repo", last_daiv_sha="abc123")
    callback = make_callback()

    assert callback.accept_callback() is True
    await callback.process_callback()

    event = await InboxEvent.objects.aget(session_id=ownership.session_id)
    assert (event.kind, event.wake, event.reason) == (EventKind.CI_FAILED, False, QuietReason.WATCH_OFF)


@pytest.fixture
def stub_enqueue(monkeypatch):
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("sessions.tasks.evaluate_pipeline_watch_task", FakeTask())
    return enqueued


@pytest.mark.django_db(transaction=True)
async def test_processing_enqueues_an_evaluation(monkeypatch_dependencies, stub_enqueue):
    await amake_owned_merge_request(thread_id="pr-thread", repo_id="owner/repo")

    await make_callback().process_callback()

    assert stub_enqueue == [{"repo_id": "owner/repo", "ref": "daiv/branch", "pipeline_id": 100}]


@pytest.mark.django_db(transaction=True)
async def test_a_branch_with_no_watch_costs_no_task(monkeypatch_dependencies, stub_enqueue):
    """An unwatched branch must not cost an interactive-queue round-trip."""
    await make_callback().process_callback()

    assert stub_enqueue == []
