import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from activity.models import Activity
from django_tasks_db.models import DBTaskResult, get_date_max

from codebase.clients.gitlab.api.callbacks import IssueCallback, NoteCallback
from codebase.clients.gitlab.api.models import (
    Issue,
    IssueAction,
    Label,
    MergeRequest,
    Note,
    NoteableType,
    NoteAction,
    Project,
    User,
)
from codebase.repo_config import RepositoryConfig

# ---------------------------------------------------------------------------
# Unit truth table for ``has_max_label()`` (label-matching logic).
# ---------------------------------------------------------------------------


def _label(title: str) -> Label:
    return Label(title=title)


def _issue(labels: list[Label]) -> Issue:
    return Issue(id=1, iid=1, title="t", description="", state="opened", assignee_id=None, labels=labels, type="Issue")


def _merge_request(labels: list[Label]) -> MergeRequest:
    return MergeRequest(
        id=1, iid=1, title="t", state="opened", source_branch="feature", target_branch="main", labels=labels
    )


def test_issue_has_max_label_true():
    assert _issue([_label("daiv-max")]).has_max_label() is True


def test_issue_has_max_label_case_insensitive():
    assert _issue([_label("DAIV-Max")]).has_max_label() is True


def test_issue_has_max_label_false_when_absent():
    assert _issue([_label("daiv")]).has_max_label() is False


def test_merge_request_has_max_label_true():
    assert _merge_request([_label("daiv-max")]).has_max_label() is True


def test_merge_request_has_max_label_false_when_absent():
    assert _merge_request([_label("daiv")]).has_max_label() is False


# ---------------------------------------------------------------------------
# End-to-end: ``process_callback`` persists ``use_max`` onto the Activity row.
# Guards against silent drift where a callback forgets to forward has_max_label().
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
    current_user = MagicMock(id=1, username="daiv")

    def create_issue_emoji(self, *_a, **_kw):
        pass

    def create_merge_request_note_emoji(self, *_a, **_kw):
        pass

    def has_issue_reaction(self, *_a, **_kw):
        return False


@pytest.fixture
def _stub_gitlab(monkeypatch):
    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.RepoClient.create_instance", lambda: _StubClient())
    monkeypatch.setattr(
        "codebase.clients.gitlab.api.callbacks.RepositoryConfig.get_config", lambda *_a, **_kw: RepositoryConfig()
    )
    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.resolve_user", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("labels, expected", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)])
async def test_issue_callback_persists_use_max(_stub_gitlab, labels, expected):
    task_id = await _make_db_task_result()
    callback = IssueCallback(
        object_kind="issue",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
        object_attributes=Issue(
            id=100,
            iid=42,
            title="T",
            description="",
            state="opened",
            assignee_id=None,
            action=IssueAction.OPEN,
            labels=labels,
            type="Issue",
        ),
    )
    with patch("codebase.clients.gitlab.api.callbacks.address_issue_task") as mock_task:
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("labels, expected", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)])
async def test_note_callback_on_mr_persists_use_max(_stub_gitlab, labels, expected):
    task_id = await _make_db_task_result()
    callback = NoteCallback(
        object_kind="note",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
        merge_request=MergeRequest(
            id=10, iid=7, title="MR", state="opened", source_branch="feat", target_branch="main", labels=labels
        ),
        object_attributes=Note(
            id=500,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.MERGE_REQUEST,
            noteable_id=7,
            discussion_id="d1",
            note="@daiv please look",
            system=False,
        ),
    )
    with (
        patch("codebase.clients.gitlab.api.callbacks.address_mr_comments_task") as mock_task,
        patch("codebase.clients.gitlab.api.callbacks.note_mentions_daiv", return_value=True),
    ):
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("labels, expected", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)])
async def test_note_callback_on_issue_persists_use_max(_stub_gitlab, labels, expected):
    task_id = await _make_db_task_result()
    callback = NoteCallback(
        object_kind="note",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
        issue=Issue(
            id=100, iid=42, title="T", description="", state="opened", assignee_id=None, labels=labels, type="Issue"
        ),
        object_attributes=Note(
            id=500,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.ISSUE,
            noteable_id=42,
            discussion_id="d1",
            note="@daiv please look",
            system=False,
        ),
    )
    with (
        patch("codebase.clients.gitlab.api.callbacks.address_issue_task") as mock_task,
        patch("codebase.clients.gitlab.api.callbacks.note_mentions_daiv", return_value=True),
    ):
        mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
        await callback.process_callback()

    activity = await Activity.objects.aget(task_result_id=task_id)
    assert activity.use_max is expected
