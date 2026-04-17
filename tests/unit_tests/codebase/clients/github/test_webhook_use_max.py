import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from activity.models import Activity
from django_tasks_db.models import DBTaskResult, get_date_max

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
# End-to-end: ``process_callback`` persists ``use_max`` on the Activity row.
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
    "labels, expected", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_issue_callback_persists_use_max(_stub_github, labels, expected):
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

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expected", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_issue_comment_callback_persists_use_max(_stub_github, labels, expected):
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

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expected", [([Label(id=1, name="daiv-max")], True), ([Label(id=1, name="daiv")], False)]
)
async def test_pr_comment_callback_persists_use_max(_stub_github, labels, expected):
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

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected
