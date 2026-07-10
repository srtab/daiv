import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from django_tasks_db.models import DBTaskResult, get_date_max
from sessions.models import Run, Session

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
# End-to-end: ``process_callback`` persists model-pair onto the Run row.
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
@pytest.mark.parametrize(
    "labels, expect_max_model", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)]
)
async def test_issue_callback_persists_agent_model(_stub_gitlab, labels, expect_max_model):
    from core.site_settings import site_settings

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
    "labels, expect_max_model", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)]
)
async def test_note_callback_on_mr_persists_agent_model(_stub_gitlab, labels, expect_max_model):
    from core.site_settings import site_settings

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

    run = await Run.objects.aget(task_result_id=task_id)
    if expect_max_model:
        assert run.agent_model == site_settings.agent_max_model_name
    else:
        assert run.agent_model == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "labels, expect_max_model", [([Label(title="daiv-max")], True), ([Label(title="daiv")], False)]
)
async def test_note_callback_on_issue_persists_agent_model(_stub_gitlab, labels, expect_max_model):
    from core.site_settings import site_settings

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

    run = await Run.objects.aget(task_result_id=task_id)
    if expect_max_model:
        assert run.agent_model == site_settings.agent_max_model_name
    else:
        assert run.agent_model == ""


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_two_issue_events_share_one_session(_stub_gitlab):
    """Two callbacks for the same issue produce ONE Session and TWO Runs."""
    from codebase.base import Scope
    from codebase.utils import compute_thread_id

    issue_iid = 99
    repo = "group/two-event-repo"
    thread_id = compute_thread_id(repo_slug=repo, scope=Scope.ISSUE, entity_iid=issue_iid)

    for _n in range(2):
        task_id = await _make_db_task_result()
        callback = IssueCallback(
            object_kind="issue",
            project=Project(id=1, path_with_namespace=repo, default_branch="main"),
            user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
            object_attributes=Issue(
                id=100,
                iid=issue_iid,
                title="T",
                description="",
                state="opened",
                assignee_id=None,
                action=IssueAction.OPEN,
                labels=[Label(title="daiv")],
                type="Issue",
            ),
        )
        with patch("codebase.clients.gitlab.api.callbacks.address_issue_task") as mock_task:
            mock_task.aenqueue = AsyncMock(return_value=MagicMock(id=task_id))
            await callback.process_callback()

    assert await Session.objects.filter(thread_id=thread_id).acount() == 1
    assert await Run.objects.filter(session_id=thread_id).acount() == 2
