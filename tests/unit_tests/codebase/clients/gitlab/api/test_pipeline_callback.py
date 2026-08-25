import pytest

from codebase.clients.gitlab.api.callbacks import PipelineCallback
from codebase.clients.gitlab.api.models import PipelineEvent, Project, User
from codebase.repo_config import RepositoryConfig


class StubClient:
    def __init__(self):
        self.current_user = User(id=1, username="daiv", name="DAIV", email="daiv@example.com")


@pytest.fixture
def stub_client():
    return StubClient()


@pytest.fixture
def repo_config():
    return RepositoryConfig()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, stub_client, repo_config):
    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr(
        "codebase.clients.gitlab.api.callbacks.RepositoryConfig.get_config", lambda *a, **kw: repo_config
    )


def make_callback(status: str, *, user_id: int = 10, ref: str = "daiv/branch") -> PipelineCallback:
    return PipelineCallback(
        object_kind="pipeline",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=user_id, username="someone", name="Someone", email="someone@example.com"),
        object_attributes=PipelineEvent(id=100, iid=1, ref=ref, sha="abc123", status=status),
    )


@pytest.mark.parametrize("status", ["success", "failed", "canceled", "skipped"])
def test_terminal_statuses_are_accepted(status, monkeypatch_dependencies):
    assert make_callback(status).accept_callback() is True


@pytest.mark.parametrize("status", ["created", "pending", "running", "waiting_for_resource", "preparing"])
def test_transitional_statuses_are_ignored(status, monkeypatch_dependencies):
    assert make_callback(status).accept_callback() is False


def test_a_pipeline_daiv_triggered_is_still_accepted(monkeypatch_dependencies, stub_client):
    """The regression test that matters.

    On repos using ephemeral push tokens the publisher pushes with [skip ci] and heals CI as
    the service account, so the pipeline we must watch is attributed to DAIV itself. Every
    other callback in this codebase rejects self-attributed events; copying that here makes
    the whole feature a no-op on exactly those repos.
    """
    callback = make_callback("failed", user_id=stub_client.current_user.id)
    assert callback.accept_callback() is True


def test_a_repo_with_the_watch_disabled_is_ignored(monkeypatch_dependencies, repo_config):
    repo_config.pipeline_watch.enabled = False
    assert make_callback("failed").accept_callback() is False


async def test_processing_enqueues_an_evaluation(monkeypatch_dependencies, monkeypatch):
    enqueued = []

    class FakeTask:
        async def aenqueue(self, **kwargs):
            enqueued.append(kwargs)

    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.evaluate_pipeline_watch_task", FakeTask())
    await make_callback("failed").process_callback()

    assert enqueued == [{"repo_id": "group/repo", "ref": "daiv/branch", "pipeline_id": 100}]
