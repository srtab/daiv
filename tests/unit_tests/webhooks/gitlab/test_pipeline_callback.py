from unittest.mock import patch

import pytest
from sessions.models import EventKind, InboxEvent, QuietReason
from webhooks.gitlab.callbacks import PipelineCallback
from webhooks.gitlab.models import PipelineEvent, Project, User

from codebase.repo_config import RepositoryConfig
from core.site_settings import site_settings
from tests.unit_tests.conftest import site_snapshot
from tests.unit_tests.sessions.conftest import amake_owned_merge_request


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
    monkeypatch.setattr("webhooks.gitlab.callbacks.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr("webhooks.gitlab.callbacks.RepositoryConfig.get_config", lambda *a, **kw: repo_config)


def make_callback(
    status: str, *, user_id: int = 10, ref: str = "daiv/branch", source: str | None = "push"
) -> PipelineCallback:
    return PipelineCallback(
        object_kind="pipeline",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=user_id, username="someone", name="Someone", email="someone@example.com"),
        object_attributes=PipelineEvent(id=100, iid=1, ref=ref, sha="abc123", status=status, source=source),
    )


@pytest.mark.parametrize("status", ["success", "failed", "canceled", "skipped"])
def test_terminal_statuses_are_accepted(status, monkeypatch_dependencies):
    assert make_callback(status).accept_callback() is True


@pytest.mark.parametrize("status", ["created", "pending", "running", "waiting_for_resource", "preparing"])
def test_transitional_statuses_are_ignored(status, monkeypatch_dependencies):
    assert make_callback(status).accept_callback() is False


@pytest.mark.parametrize("status", ["success", "failed"])
def test_a_child_pipeline_is_ignored(status, monkeypatch_dependencies):
    """A child pipeline runs on its parent's ref and commit with a higher id, so judging it could hide the parent's
    later failure; the merge request's result is the parent's, which a ``strategy: depend`` child reaches."""
    assert make_callback(status, source="parent_pipeline").accept_callback() is False


def test_a_pipeline_whose_source_is_not_named_is_accepted(monkeypatch_dependencies):
    assert make_callback("failed", source=None).accept_callback() is True


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


@pytest.mark.parametrize("switch", ["repository", "site"])
def test_a_pipeline_is_accepted_with_the_watch_turned_off(monkeypatch_dependencies, repo_config, switch):
    """A watch turned off still remembers CI results, so whether one wakes the session is the evaluation's call."""
    repo_config.pipeline_watch.enabled = switch != "repository"
    site = site_snapshot(pipeline_watch_enabled=switch != "site")

    with patch.object(site_settings, "snapshot", return_value=site):
        assert make_callback("failed").accept_callback() is True


@pytest.mark.django_db(transaction=True)
async def test_a_failure_with_the_watch_off_is_remembered(monkeypatch_dependencies, repo_config, evaluate_inline):
    repo_config.pipeline_watch.enabled = False
    ownership = await amake_owned_merge_request(last_daiv_sha="abc123")
    callback = make_callback("failed")

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
    await amake_owned_merge_request()

    await make_callback("failed").process_callback()

    assert stub_enqueue == [{"repo_id": "group/repo", "ref": "daiv/branch", "pipeline_id": 100}]


@pytest.mark.django_db(transaction=True)
async def test_a_branch_with_no_watch_costs_no_task(monkeypatch_dependencies, stub_enqueue):
    """An unwatched branch must not cost an interactive-queue round-trip."""
    await make_callback("failed").process_callback()

    assert stub_enqueue == []
