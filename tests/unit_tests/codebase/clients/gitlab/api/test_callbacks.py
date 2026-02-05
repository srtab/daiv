import pytest

from codebase.base import Discussion
from codebase.base import Note as BaseNote
from codebase.base import User as BaseUser
from codebase.clients.gitlab.api.callbacks import IssueCallback, NoteCallback
from codebase.clients.gitlab.api.models import (
    Issue,
    IssueAction,
    IssueChanges,
    Label,
    LabelChange,
    MergeRequest,
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


@pytest.fixture
def stub_client():
    return StubClient()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, stub_client):
    """Monkeypatch RepoClient and RepositoryConfig for testing."""
    monkeypatch.setattr("codebase.clients.gitlab.api.callbacks.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr(
        "codebase.clients.gitlab.api.callbacks.RepositoryConfig.get_config", lambda *args, **kwargs: RepositoryConfig()
    )


def create_note_callback(note_body: str) -> NoteCallback:
    """Helper to create a minimal NoteCallback instance."""
    return NoteCallback(
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
    action: IssueAction, issue_labels: list[Label], issue_state: str = "opened", changes: IssueChanges | None = None
) -> IssueCallback:
    """Helper to create an IssueCallback instance."""
    return IssueCallback(
        object_kind="issue",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
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
