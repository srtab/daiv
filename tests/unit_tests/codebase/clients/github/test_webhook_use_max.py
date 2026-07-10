import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.models import Run, Session

from codebase.clients.github.api.callbacks import IssueCallback, IssueCommentCallback
from codebase.clients.github.api.models import Comment, Issue, Label, PullRequest, Ref, Repository, User
from codebase.repo_config import RepositoryConfig

# ---------------------------------------------------------------------------
# Unit truth table for ``has_max_label()``.
# ---------------------------------------------------------------------------


def _label(name: str) -> Label:
    return Label(id=1, name=name)


def _issue(labels: list[Label], pull_request: dict | None = None) -> Issue:
    return Issue(id=1, number=1, title="t", state="open", labels=labels, pull_request=pull_request)


def _pull_request(labels: list[Label]) -> PullRequest:
    return PullRequest(
        id=1,
        number=1,
        title="t",
        state="open",
        head=Ref(ref="feature", sha="abc"),
        base=Ref(ref="main", sha="def"),
        labels=labels,
    )


def test_issue_has_max_label_true():
    assert _issue([_label("daiv-max")]).has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    assert _issue([_label("DAIV-MAX")]).has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    assert _issue([_label("daiv")]).has_max_label() is False


def test_pull_request_has_max_label_true():
    assert _pull_request([_label("daiv-max")]).has_max_label() is True


def test_pull_request_has_max_label_false_when_absent():
    assert _pull_request([_label("daiv")]).has_max_label() is False


# ---------------------------------------------------------------------------
# End-to-end: ``process_callback`` persists model-pair on the Run row.
# ---------------------------------------------------------------------------


async def _make_db_task_result() -> uuid.UUID:
    task_id = uuid.uuid4()
    await DBTaskResult.objects.acreate(
        id=task_id,
        status="READY",
        task_path="codebase.tasks.address_issue_task",
        args_kwargs={"args": [], "kwargs": {}},
        queue_name="default",
        backend_name="default",
        run_after=get_date_max(),
        return_value={},
    )
    return task_id


class _StubClient:
    current_user = MagicMock(id=999, username="daiv")

    def create_issue_emoji(self, *_a, **_kw):
        pass

    def create_merge_request_note_emoji(self, *_a, **_kw):
        pass

    def has_issue_reaction(self, *_a, **_kw):
        return False


@pytest.fixture
def _stub_github(monkeypatch):
    monkeypatch.setattr("codebase.clients.github.api.callbacks.RepoClient.create_instance", lambda: _StubClient())
    monkeypatch.setattr(
        "codebase.clients.github.api.callbacks.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )
    monkeypatch.setattr("codebase.clients.github.api.callbacks.resolve_user", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expect_max_model", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_issue_callback_persists_agent_model(_stub_github, labels, expect_max_model):
    from core.site_settings import site_settings

    task_id = await _make_db_task_result()
    callback = IssueCallback(
        action="opened",
        repository=Repository(id=1, full_name="acme/repo", default_branch="main"),
        issue=_issue(labels),
        sender=User(id=2, login="reviewer"),
    )
    with patch("codebase.clients.github.api.callbacks.address_issue_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    run = await Run.objects.aget(task_result_id=task_id)
    if expect_max_model:
        assert run.agent_model == site_settings.agent_max_model_name
        assert run.agent_thinking_level == site_settings.agent_max_thinking_level
    else:
        assert run.agent_model == ""
        assert run.agent_thinking_level == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expect_max_model", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_issue_comment_callback_persists_agent_model(_stub_github, labels, expect_max_model):
    from core.site_settings import site_settings

    task_id = await _make_db_task_result()
    callback = IssueCommentCallback(
        action="created",
        repository=Repository(id=1, full_name="acme/repo", default_branch="main"),
        issue=_issue(labels),
        comment=Comment(id=500, body="@daiv look", user=User(id=2, login="reviewer")),
    )
    with (
        patch("codebase.clients.github.api.callbacks.address_issue_task") as mock_task,
        patch("codebase.clients.github.api.callbacks.note_mentions_daiv", return_value=True),
    ):
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    run = await Run.objects.aget(task_result_id=task_id)
    if expect_max_model:
        assert run.agent_model == site_settings.agent_max_model_name
    else:
        assert run.agent_model == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expect_max_model", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_pr_comment_callback_persists_agent_model(_stub_github, labels, expect_max_model):
    from core.site_settings import site_settings

    task_id = await _make_db_task_result()
    # GitHub webhooks treat PR comments as Issue comments, so ``issue`` here carries the PR-stub dict.
    pr_stub_issue = _issue(labels, pull_request={"url": "https://example/pr/1"})
    callback = IssueCommentCallback(
        action="created",
        repository=Repository(id=1, full_name="acme/repo", default_branch="main"),
        issue=pr_stub_issue,
        comment=Comment(id=500, body="@daiv look", user=User(id=2, login="reviewer")),
    )
    with (
        patch("codebase.clients.github.api.callbacks.address_mr_comments_task") as mock_task,
        patch("codebase.clients.github.api.callbacks.note_mentions_daiv", return_value=True),
    ):
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    run = await Run.objects.aget(task_result_id=task_id)
    if expect_max_model:
        assert run.agent_model == site_settings.agent_max_model_name
    else:
        assert run.agent_model == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_two_issue_events_share_one_session(_stub_github):
    """Two callbacks for the same issue produce ONE Session and TWO Runs."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    issue_number = 77
    repo = "acme/two-event-repo"
    thread_id = compute_thread_id(repo_slug=repo, scope=Scope.ISSUE, entity_iid=issue_number)

    for _n in range(2):
        task_id = await _make_db_task_result()
        callback = IssueCallback(
            action="opened",
            repository=Repository(id=1, full_name=repo, default_branch="main"),
            issue=_issue([Label(id=1, name="daiv")]),
            sender=User(id=2, login="reviewer"),
        )
        callback.issue = _issue([Label(id=1, name="daiv")])
        with patch("codebase.clients.github.api.callbacks.address_issue_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
            # Patch compute_thread_id to use our specific repo/issue
            with patch("codebase.clients.github.api.callbacks.compute_thread_id", return_value=thread_id):
                await callback.process_callback()

    assert await Session.objects.filter(thread_id=thread_id).acount() == 1
    assert await Run.objects.filter(session_id=thread_id).acount() == 2
