import pytest

from codebase.base import Discussion
from codebase.base import Note as BaseNote
from codebase.base import User as BaseUser
from codebase.clients.gitlab.api.callbacks import IssueCallback, MergeRequestCallback, NoteCallback
from codebase.clients.gitlab.api.models import (
    Issue,
    IssueAction,
    IssueChanges,
    Label,
    LabelChange,
    MergeRequest,
    MergeRequestAction,
    MergeRequestEvent,
    Note,
    NoteableType,
    NoteAction,
    Project,
    User,
)
from codebase.repo_config import RepositoryConfig


class StubClient:
    def __init__(self):
        self.current_user = BaseUser(id=1, username="daiv", name="DAIV")
        self._discussion = None
        self._has_reaction = False

    def get_merge_request_comment(self, *_a, **_kw):
        return self._discussion

    def set_discussion(self, discussion):
        self._discussion = discussion

    def has_issue_reaction(self, *_a, **_kw):
        return self._has_reaction

    def set_has_reaction(self, value):
        self._has_reaction = value

    def create_issue_emoji(self, *_a, **_kw):
        pass

    def create_merge_request_note_emoji(self, *_a, **_kw):
        pass


@pytest.fixture
def stub_client():
    return StubClient()


@pytest.fixture
def repo_config():
    """Mutable RepositoryConfig instance."""
    return RepositoryConfig()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, stub_client, repo_config):
    """Monkeypatch RepoClient and RepositoryConfig for testing."""
    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr(
        "codebase.clients.gitlab.api.callbacks.RepositoryConfig.get_config", lambda *args, **kwargs: repo_config
    )


def create_note_callback(note_body: str, username: str = "reviewer") -> NoteCallback:
    """Helper to create a minimal NoteCallback instance."""
    return NoteCallback(
        object_kind="note",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username=username, name="Reviewer", email=f"{username}@example.com"),
        merge_request=MergeRequest(
            id=10,
            iid=1,
            title="Some MR",
            state="opened",
            work_in_progress=False,
            source_branch="feat",
            target_branch="main",
            labels=[],
        ),
        object_attributes=Note(
            id=100,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.MERGE_REQUEST,
            noteable_id=1,
            discussion_id="discussion_1",
            note=note_body,
            system=False,
        ),
    )


def test_accept_when_mention(monkeypatch_dependencies, stub_client):
    """Test that callback is accepted when note body contains @daiv mention."""
    callback = create_note_callback("@daiv please review this code")

    assert callback.accept_callback() is True


def test_reject_when_thread_only_has_daiv(monkeypatch_dependencies, stub_client):
    """Test that callback is rejected when discussion thread has only DAIV-authored notes."""
    callback = create_note_callback("This looks good to me")

    # Create a discussion with the current note and a DAIV-authored note
    discussion = Discussion(
        id="discussion_1",
        notes=[
            BaseNote(
                id=100,  # This is the incoming note
                body="This looks good to me",
                author=BaseUser(id=2, username="reviewer", name="Reviewer"),
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            BaseNote(
                id=99,  # Previous DAIV note in same discussion
                body="I've updated the code based on your feedback",
                author=BaseUser(id=1, username="daiv", name="DAIV"),
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ],
    )

    stub_client.set_discussion(discussion)

    assert callback.accept_callback() is False


def test_reject_unmentioned_and_no_daiv_thread(monkeypatch_dependencies, stub_client):
    """Test that callback is rejected when no mention and discussion has no DAIV notes."""
    callback = create_note_callback("This looks good to me")

    # Create a discussion with only non-DAIV notes
    discussion = Discussion(
        id="discussion_1",
        notes=[
            BaseNote(
                id=100,  # This is the incoming note
                body="This looks good to me",
                author=BaseUser(id=2, username="reviewer", name="Reviewer"),
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
            BaseNote(
                id=101,  # Another reviewer note
                body="I agree with the changes",
                author=BaseUser(id=3, username="other_reviewer", name="Other Reviewer"),
                noteable_type=NoteableType.MERGE_REQUEST,
                system=False,
                resolvable=False,
            ),
        ],
    )

    stub_client.set_discussion(discussion)

    assert callback.accept_callback() is False


def test_reject_when_bare_daiv_mention(monkeypatch_dependencies, stub_client):
    """Test that callback is rejected when note body contains bare DAIV reference."""
    callback = create_note_callback("DAIV please fix this issue")

    stub_client.set_discussion(Discussion(id="discussion_1", notes=[]))

    assert callback.accept_callback() is False


def test_reject_when_note_by_daiv_itself(monkeypatch_dependencies, stub_client):
    """Test that callback is rejected when the note is created by DAIV itself."""
    callback = NoteCallback(
        object_kind="note",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=1, username="daiv", name="DAIV", email="daiv@example.com"),  # DAIV user
        merge_request=MergeRequest(
            id=10,
            iid=1,
            title="Some MR",
            state="opened",
            work_in_progress=False,
            source_branch="feat",
            target_branch="main",
            labels=[],
        ),
        object_attributes=Note(
            id=100,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.MERGE_REQUEST,
            noteable_id=1,
            discussion_id="discussion_1",
            note="I've updated the code",
            system=False,
        ),
    )

    assert callback.accept_callback() is False


def test_reject_when_system_note(monkeypatch_dependencies, stub_client):
    """Test that callback is rejected when note is a system note."""
    callback = NoteCallback(
        object_kind="note",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
        merge_request=MergeRequest(
            id=10,
            iid=1,
            title="Some MR",
            state="opened",
            work_in_progress=False,
            source_branch="feat",
            target_branch="main",
            labels=[],
        ),
        object_attributes=Note(
            id=100,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.MERGE_REQUEST,
            noteable_id=1,
            discussion_id="discussion_1",
            note="@daiv please review",
            system=True,  # System note
        ),
    )

    assert callback.accept_callback() is False


def create_issue_callback(
    action: IssueAction,
    issue_labels: list[Label],
    issue_state: str = "opened",
    changes: IssueChanges | None = None,
    username: str = "testuser",
) -> IssueCallback:
    """Helper to create an IssueCallback instance."""
    return IssueCallback(
        object_kind="issue",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=10, username=username, name="Test User", email=f"{username}@example.com"),
        object_attributes=Issue(
            id=100,
            iid=42,
            title="Test Issue",
            description="Test description",
            state=issue_state,
            assignee_id=None,
            action=action,
            labels=issue_labels,
            type="Issue",
        ),
        changes=changes,
    )


class TestIssueCallback:
    """Tests for GitLab IssueCallback."""

    def test_accept_callback_opened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is opened with a DAIV label."""
        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")])
        assert callback.accept_callback() is True

    def test_accept_callback_update_with_daiv_label_added(self, monkeypatch_dependencies):
        """Test that callback is accepted when a DAIV label is added in an update."""
        changes = IssueChanges(
            labels=LabelChange(previous=[Label(title="bug")], current=[Label(title="bug"), Label(title="daiv")])
        )
        callback = create_issue_callback(
            action=IssueAction.UPDATE, issue_labels=[Label(title="bug"), Label(title="daiv")], changes=changes
        )
        assert callback.accept_callback() is True

    def test_accept_callback_update_with_daiv_auto_label_added(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-auto label is added."""
        changes = IssueChanges(labels=LabelChange(previous=[], current=[Label(title="daiv-auto")]))
        callback = create_issue_callback(
            action=IssueAction.UPDATE, issue_labels=[Label(title="daiv-auto")], changes=changes
        )
        assert callback.accept_callback() is True

    def test_accept_callback_update_with_daiv_max_label_added(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-max label is added."""
        changes = IssueChanges(labels=LabelChange(previous=[], current=[Label(title="daiv-max")]))
        callback = create_issue_callback(
            action=IssueAction.UPDATE, issue_labels=[Label(title="daiv-max")], changes=changes
        )
        assert callback.accept_callback() is True

    def test_reject_callback_update_with_non_daiv_label_added(self, monkeypatch_dependencies):
        """Test that callback is rejected when a non-DAIV label is added."""
        changes = IssueChanges(labels=LabelChange(previous=[], current=[Label(title="bug")]))
        callback = create_issue_callback(action=IssueAction.UPDATE, issue_labels=[Label(title="bug")], changes=changes)
        assert callback.accept_callback() is False

    def test_reject_callback_update_with_daiv_label_removed(self, monkeypatch_dependencies):
        """Test that callback is rejected when a DAIV label is removed."""
        changes = IssueChanges(
            labels=LabelChange(previous=[Label(title="daiv"), Label(title="bug")], current=[Label(title="bug")])
        )
        callback = create_issue_callback(action=IssueAction.UPDATE, issue_labels=[Label(title="bug")], changes=changes)
        assert callback.accept_callback() is False

    def test_reject_callback_update_without_label_changes(self, monkeypatch_dependencies):
        """Test that callback is rejected when update doesn't include label changes."""
        callback = create_issue_callback(action=IssueAction.UPDATE, issue_labels=[Label(title="daiv")], changes=None)
        assert callback.accept_callback() is False

    def test_reject_callback_when_already_reacted(self, monkeypatch_dependencies, stub_client):
        """Test that callback is rejected when DAIV has already reacted to the issue."""
        stub_client.set_has_reaction(True)

        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")])
        assert callback.accept_callback() is False

    def test_reject_callback_closed_issue(self, monkeypatch_dependencies):
        """Test that callback is rejected for closed issues."""
        callback = create_issue_callback(
            action=IssueAction.OPEN, issue_labels=[Label(title="daiv")], issue_state="closed"
        )
        assert callback.accept_callback() is False

    def test_reject_callback_work_item(self, monkeypatch_dependencies):
        """Test that callback is rejected for work items."""
        IssueCallback(
            object_kind="work_item",
            project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
            user=User(id=10, username="testuser", name="Test User", email="testuser@example.com"),
            object_attributes=Issue(
                id=100,
                iid=42,
                title="Test Work Item",
                description="Test description",
                state="opened",
                assignee_id=None,
                action=IssueAction.OPEN,
                labels=[Label(title="daiv")],
                type="Task",
            ),
        )

    def test_label_check_case_insensitive(self, monkeypatch_dependencies):
        """Test that label checking is case-insensitive."""
        changes = IssueChanges(labels=LabelChange(previous=[], current=[Label(title="DAIV")]))
        callback = create_issue_callback(action=IssueAction.UPDATE, issue_labels=[Label(title="DAIV")], changes=changes)
        assert callback.accept_callback() is True

    def test_reject_callback_user_not_in_allowlist(self, monkeypatch_dependencies, repo_config):
        """Test that callback is rejected when user is not in the allowed usernames list."""
        repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(
            action=IssueAction.OPEN, issue_labels=[Label(title="daiv")], username="mallory"
        )
        assert callback.accept_callback() is False

    def test_accept_callback_user_in_allowlist(self, monkeypatch_dependencies, repo_config):
        """Test that callback is accepted when user is in the allowed usernames list."""
        repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")], username="alice")
        assert callback.accept_callback() is True

    def test_accept_callback_empty_allowlist(self, monkeypatch_dependencies):
        """Test that callback is accepted when allowlist is empty (all users allowed)."""
        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")])
        assert callback.accept_callback() is True

    def test_allowlist_case_insensitive(self, monkeypatch_dependencies, repo_config):
        """Test that allowlist check is case-insensitive."""
        repo_config.allowed_usernames = ("Alice",)

        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")], username="alice")
        assert callback.accept_callback() is True


class TestNoteCallbackAllowlist:
    """Tests for GitLab NoteCallback allowlist."""

    def test_reject_note_user_not_in_allowlist(self, monkeypatch_dependencies, repo_config):
        """Test that note callback is rejected when user is not in the allowed usernames list."""
        repo_config.allowed_usernames = ("alice",)

        callback = create_note_callback("@daiv please review this code", username="mallory")
        assert callback.accept_callback() is False

    def test_accept_note_user_in_allowlist(self, monkeypatch_dependencies, repo_config):
        """Test that note callback is accepted when user is in the allowed usernames list."""
        repo_config.allowed_usernames = ("reviewer",)

        callback = create_note_callback("@daiv please review this code")
        assert callback.accept_callback() is True


def create_merge_request_callback(
    action: MergeRequestAction = MergeRequestAction.MERGE, state: str = "merged", target_branch: str = "main"
) -> MergeRequestCallback:
    """Helper to create a MergeRequestCallback instance."""
    return MergeRequestCallback(
        object_kind="merge_request",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        user=User(id=2, username="developer", name="Developer", email="developer@example.com"),
        object_attributes=MergeRequestEvent(
            id=10,
            iid=1,
            title="Some MR",
            state=state,
            action=action,
            source_branch="feat/something",
            target_branch=target_branch,
            author_id=2,
            merged_at="2026-04-01T10:00:00Z",
        ),
    )


class TestMergeRequestCallback:
    """Tests for GitLab MergeRequestCallback."""

    def test_accept_callback_on_merge(self):
        """Test that callback is accepted when MR is merged."""
        callback = create_merge_request_callback()
        assert callback.accept_callback() is True

    def test_reject_callback_on_open(self):
        """Test that callback is rejected when MR is opened."""
        callback = create_merge_request_callback(action=MergeRequestAction.OPEN, state="opened")
        assert callback.accept_callback() is False

    def test_reject_callback_on_close(self):
        """Test that callback is rejected when MR is closed."""
        callback = create_merge_request_callback(action=MergeRequestAction.CLOSE, state="closed")
        assert callback.accept_callback() is False

    def test_reject_callback_on_update(self):
        """Test that callback is rejected when MR is updated."""
        callback = create_merge_request_callback(action=MergeRequestAction.UPDATE, state="opened")
        assert callback.accept_callback() is False

    def test_reject_callback_when_state_not_merged(self):
        """Test that callback is rejected when action is merge but state is not merged."""
        callback = create_merge_request_callback(action=MergeRequestAction.MERGE, state="opened")
        assert callback.accept_callback() is False

    def test_reject_callback_when_target_not_default_branch(self):
        """Test that callback is rejected when MR targets a non-default branch."""
        callback = create_merge_request_callback(target_branch="develop")
        assert callback.accept_callback() is False

    async def test_process_callback_enqueues_task(self):
        """Test that process_callback enqueues the merge metrics task with correct args."""
        from unittest.mock import AsyncMock, patch

        callback = create_merge_request_callback()
        with patch("codebase.tasks.record_merge_metrics_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_called_once_with(
            repo_id="group/repo",
            merge_request_iid=1,
            title="Some MR",
            source_branch="feat/something",
            target_branch="main",
            merged_at="2026-04-01T10:00:00Z",
            platform="gitlab",
        )

    def test_reject_callback_when_default_branch_is_none(self):
        """Test that callback is rejected when project has no default branch."""
        callback = MergeRequestCallback(
            object_kind="merge_request",
            project=Project(id=1, path_with_namespace="group/repo", default_branch=None),
            user=User(id=2, username="developer", name="Developer", email="developer@example.com"),
            object_attributes=MergeRequestEvent(
                id=10,
                iid=1,
                title="Some MR",
                state="merged",
                action=MergeRequestAction.MERGE,
                source_branch="feat/something",
                target_branch="main",
                author_id=2,
            ),
        )
        assert callback.accept_callback() is False

    async def test_process_callback_coalesces_none_merged_at(self):
        """Test that process_callback passes empty string when merged_at is None."""
        from unittest.mock import AsyncMock, patch

        callback = MergeRequestCallback(
            object_kind="merge_request",
            project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
            user=User(id=2, username="developer", name="Developer", email="developer@example.com"),
            object_attributes=MergeRequestEvent(
                id=10,
                iid=1,
                title="Some MR",
                state="merged",
                action=MergeRequestAction.MERGE,
                source_branch="feat/something",
                target_branch="main",
                author_id=2,
                merged_at=None,
            ),
        )
        with patch("codebase.tasks.record_merge_metrics_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["merged_at"] == ""


@pytest.mark.django_db
class TestProcessCallbackThreadId:
    """The deterministic thread_id minted in the callback must reach both the task
    and the Activity row, so the Activity can later be joined to LangSmith traces."""

    async def test_issue_callback_passes_thread_id(self, monkeypatch_dependencies):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title="daiv")])
        expected = compute_thread_id(repo_slug="group/repo", scope=Scope.ISSUE, entity_iid=42)

        with (
            patch("codebase.clients.gitlab.api.callbacks.address_issue_task") as mock_task,
            patch("codebase.clients.gitlab.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.gitlab.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.return_value = None
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected

    async def test_note_callback_on_mr_passes_thread_id(self, monkeypatch_dependencies):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        callback = create_note_callback("@daiv please review")
        expected = compute_thread_id(repo_slug="group/repo", scope=Scope.MERGE_REQUEST, entity_iid=1)

        with (
            patch("codebase.clients.gitlab.api.callbacks.address_mr_comments_task") as mock_task,
            patch("codebase.clients.gitlab.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.gitlab.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected

    async def test_note_callback_on_issue_passes_thread_id(self, monkeypatch_dependencies):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        callback = NoteCallback(
            object_kind="note",
            project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
            user=User(id=2, username="reviewer", name="Reviewer", email="reviewer@example.com"),
            issue=Issue(
                id=100,
                iid=7,
                title="Bug",
                description="x",
                state="opened",
                assignee_id=None,
                action=IssueAction.OPEN,
                labels=[],
                type="Issue",
            ),
            object_attributes=Note(
                id=200,
                action=NoteAction.CREATE,
                noteable_type=NoteableType.ISSUE,
                noteable_id=7,
                discussion_id="discussion_2",
                note="@daiv please look",
                system=False,
            ),
        )
        expected = compute_thread_id(repo_slug="group/repo", scope=Scope.ISSUE, entity_iid=7)

        with (
            patch("codebase.clients.gitlab.api.callbacks.address_issue_task") as mock_task,
            patch("codebase.clients.gitlab.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.gitlab.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected
