import pytest

from codebase.base import Discussion
from codebase.base import Note as BaseNote
from codebase.base import User as BaseUser
from codebase.gitlab.api.callbacks import NoteCallback
from codebase.gitlab.api.models import MergeRequest, Note, NoteableType, NoteAction, Project, User
from codebase.repo_config import RepositoryConfig


class StubClient:
    def __init__(self):
        self.current_user = BaseUser(id=1, username="daiv", name="DAIV")
        self._discussion = None

    def get_merge_request_discussion(self, *_a, **_kw):
        return self._discussion

    def set_discussion(self, discussion):
        self._discussion = discussion


@pytest.fixture
def stub_client():
    return StubClient()


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, stub_client):
    """Monkeypatch RepoClient and RepositoryConfig for testing."""
    monkeypatch.setattr("codebase.api.callbacks_gitlab.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr(
        "codebase.api.callbacks_gitlab.RepositoryConfig.get_config", lambda *args, **kwargs: RepositoryConfig()
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
