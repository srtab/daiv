import pytest

from codebase.clients.github.api.callbacks import WorkflowRunCallback
from codebase.clients.github.api.models import Repository, WorkflowRun
from codebase.repo_config import RepositoryConfig


@pytest.fixture
def repo_config():
    return RepositoryConfig()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, repo_config):
    monkeypatch.setattr(
        "codebase.clients.github.api.callbacks.RepositoryConfig.get_config", lambda *a, **kw: repo_config
    )


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


def test_a_repo_with_the_watch_disabled_is_ignored(monkeypatch_dependencies, repo_config):
    repo_config.pipeline_watch.enabled = False
    assert make_callback().accept_callback() is False


async def test_processing_enqueues_an_evaluation(monkeypatch_dependencies, monkeypatch):
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("codebase.clients.github.api.callbacks.evaluate_pipeline_watch_task", FakeTask())
    await make_callback().process_callback()

    assert enqueued == [{"repo_id": "owner/repo", "ref": "daiv/branch", "pipeline_id": 100}]
