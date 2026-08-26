import pytest

from codebase.clients.gitlab.api.callbacks import PipelineCallback
from codebase.clients.gitlab.api.models import PipelineEvent, Project, User
from codebase.repo_config import RepositoryConfig
from tests.unit_tests.sessions.conftest import amake_watched_session


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


@pytest.mark.parametrize("status", ["blocked", "manual"])
def test_statuses_waiting_on_a_human_reach_the_judge(status, monkeypatch_dependencies):
    """The state machine has an outcome for these — it ends the watch with a note — so a webhook
    filter of its own would drop them and leave the branch to the 30-minute poll."""
    assert make_callback(status).accept_callback() is True


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
    await amake_watched_session()

    await make_callback("failed").process_callback()

    assert stub_enqueue == [{"repo_id": "group/repo", "ref": "daiv/branch", "pipeline_id": 100}]


@pytest.mark.django_db(transaction=True)
async def test_a_branch_with_no_watch_costs_no_task(monkeypatch_dependencies, stub_enqueue):
    """An unwatched branch must not cost an interactive-queue round-trip."""
    await make_callback("failed").process_callback()

    assert stub_enqueue == []
