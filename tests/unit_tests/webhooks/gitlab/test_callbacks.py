from datetime import timedelta
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

from django.utils import timezone

import pytest
from sessions.models import InboxEvent
from webhooks.dispatch import Author, Mention
from webhooks.gitlab.callbacks import IssueCallback, MergeRequestCallback, NoteCallback, PushCallback
from webhooks.gitlab.models import (
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

from codebase.base import GitPlatform
from codebase.base import User as BaseUser
from codebase.repo_config import RepositoryConfig
from tests.unit_tests.sessions.conftest import amake_owned_merge_request


class StubClient:
    def __init__(self):
        self.current_user = BaseUser(id=1, username="daiv", name="DAIV")
        self._has_reaction = False

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
    monkeypatch.setattr("webhooks.gitlab.callbacks.RepoClient.create_instance", lambda: stub_client)
    monkeypatch.setattr("webhooks.gitlab.callbacks.RepositoryConfig.get_config", lambda *args, **kwargs: repo_config)


def create_note_callback(
    note_body: str,
    username: str = "reviewer",
    labels: list[str] | None = None,
    note_id: int = 100,
    note_type: Literal["DiffNote", "DiscussionNote"] | None = None,
) -> NoteCallback:
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
            labels=[Label(title=t) for t in labels or []],
        ),
        object_attributes=Note(
            id=note_id,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.MERGE_REQUEST,
            noteable_id=1,
            discussion_id="discussion_1",
            note=note_body,
            system=False,
            type=note_type,
        ),
    )


def test_accept_when_mention(monkeypatch_dependencies, stub_client):
    """Test that callback is accepted when note body contains @daiv mention."""
    callback = create_note_callback("@daiv please review this code")

    assert callback.accept_callback() is True


@pytest.mark.parametrize("note_type", ["DiscussionNote", "DiffNote", None])
@pytest.mark.parametrize("body", ["@daiv please review this code", "This looks good to me", "DAIV please fix this"])
def test_any_note_on_an_open_merge_request_is_left_for_dispatch_to_route(
    monkeypatch_dependencies, stub_client, note_type, body
):
    """Whether a session owns the merge request decides what a diff mention or a plain comment concerns."""
    assert create_note_callback(body, note_type=note_type).accept_callback() is True


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

    @pytest.mark.parametrize(
        ("action", "state"),
        [
            (MergeRequestAction.MERGE, "merged"),
            (MergeRequestAction.CLOSE, "closed"),
            (MergeRequestAction.REOPEN, "opened"),
        ],
    )
    def test_accept_a_merge_a_close_or_a_reopen(self, action, state):
        assert create_merge_request_callback(action=action, state=state).accept_callback() is True

    @pytest.mark.parametrize("action", [MergeRequestAction.OPEN, MergeRequestAction.UPDATE])
    def test_reject_the_rest(self, action):
        assert create_merge_request_callback(action=action, state="opened").accept_callback() is False

    async def test_a_merge_into_the_default_branch_records_metrics_and_tells_the_owner(self):
        callback = create_merge_request_callback()
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.gitlab.callbacks.aclose_ownership", new=AsyncMock()) as close,
        ):
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
        close.assert_awaited_once_with("group/repo", 1, merged=True, by_username="developer", target_branch="main")

    @pytest.mark.parametrize(
        ("action", "state", "target"),
        [
            (MergeRequestAction.MERGE, "merged", "develop"),
            (MergeRequestAction.MERGE, "opened", "main"),
            (MergeRequestAction.CLOSE, "closed", "main"),
        ],
        ids=["merged-elsewhere", "not-merged-yet", "closed"],
    )
    async def test_anything_but_a_merge_into_the_default_branch_records_no_metrics(self, action, state, target):
        callback = create_merge_request_callback(action=action, state=state, target_branch=target)
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.gitlab.callbacks.aclose_ownership", new=AsyncMock()) as close,
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_not_called()
        assert close.await_args.kwargs["merged"] is (action == MergeRequestAction.MERGE)

    async def test_a_project_without_a_default_branch_records_no_metrics(self):
        callback = create_merge_request_callback()
        callback.project.default_branch = None
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.gitlab.callbacks.aclose_ownership", new=AsyncMock()),
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_not_called()

    async def test_a_reopen_tells_the_owner(self):
        callback = create_merge_request_callback(action=MergeRequestAction.REOPEN, state="opened")
        with patch("webhooks.gitlab.callbacks.areopen_ownership", new=AsyncMock()) as reopen:
            await callback.process_callback()

        reopen.assert_awaited_once_with("group/repo", 1, by_username="developer")

    async def test_process_callback_coalesces_none_merged_at(self):
        callback = create_merge_request_callback()
        callback.object_attributes.merged_at = None
        with (
            patch("codebase.tasks.record_merge_metrics_task") as mock_task,
            patch("webhooks.gitlab.callbacks.aclose_ownership", new=AsyncMock()),
        ):
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["merged_at"] == ""


@pytest.mark.django_db(transaction=True)
class TestMergeRequestStateOnAnOwnedMergeRequest:
    @pytest.fixture(autouse=True)
    def _no_drain(self):
        with patch("sessions.inbox.post.drain_logged"):
            yield

    @pytest.mark.parametrize(
        ("action", "state", "kind"),
        [(MergeRequestAction.MERGE, "merged", "mr_merged"), (MergeRequestAction.CLOSE, "closed", "mr_closed")],
    )
    async def test_a_merge_or_close_is_remembered_and_stops_the_watch(self, action, state, kind):
        ownership = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=1)
        callback = create_merge_request_callback(action=action, state=state, target_branch="develop")

        await callback.process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.source_key) == (kind, False, f"mr-state:1:{state}")
        assert event.payload == {"target_branch": "develop", "by_username": "developer"}
        await ownership.arefresh_from_db()
        assert ownership.closed_at is not None

    async def test_a_reopen_watches_it_again_and_says_so(self):
        ownership = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=1, closed_at=timezone.now())

        await create_merge_request_callback(action=MergeRequestAction.REOPEN, state="opened").process_callback()

        await ownership.arefresh_from_db()
        assert ownership.closed_at is None
        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake) == ("notice", False)

    async def test_an_unowned_merge_request_posts_nothing(self):
        await create_merge_request_callback(action=MergeRequestAction.CLOSE, state="closed").process_callback()

        assert not await InboxEvent.objects.aexists()


def create_push_callback(*, ref: str = "refs/heads/feat/x", after: str = "0a1b2c3d" * 5) -> PushCallback:
    return PushCallback(
        object_kind="push",
        project=Project(id=1, path_with_namespace="group/repo", default_branch="main"),
        ref=ref,
        before="e96f1f77" * 5,
        after=after,
        checkout_sha=after,
        user_username="sfr",
        total_commits_count=2,
    )


@pytest.mark.django_db(transaction=True)
class TestPushCallback:
    async def test_a_push_to_an_owned_branch_is_weighed_a_minute_later(self):
        await amake_owned_merge_request(repo_id="group/repo", ref="feat/x")
        task = MagicMock(aenqueue=AsyncMock())

        with patch("sessions.tasks.classify_push_task") as classify:
            classify.using.return_value = task
            await create_push_callback().process_callback()

        delay = classify.using.call_args.kwargs["run_after"] - timezone.now()
        assert timedelta(seconds=50) < delay <= timedelta(seconds=60)
        task.aenqueue.assert_awaited_once_with(
            repo_id="group/repo",
            branch="feat/x",
            before="e96f1f77" * 5,
            after="0a1b2c3d" * 5,
            pusher_username="sfr",
            commit_count=2,
        )

    async def test_a_push_to_an_unowned_branch_is_ignored(self):
        with patch("sessions.tasks.classify_push_task") as classify:
            await create_push_callback().process_callback()

        classify.using.assert_not_called()

    async def test_a_deleted_branch_is_ignored(self):
        await amake_owned_merge_request(repo_id="group/repo", ref="feat/x")

        with patch("sessions.tasks.classify_push_task") as classify:
            await create_push_callback(after="0" * 40).process_callback()

        classify.using.assert_not_called()

    async def test_a_push_to_the_default_branch_only_invalidates_the_config_cache(self):
        with (
            patch("webhooks.gitlab.callbacks.RepositoryConfig.invalidate_cache") as invalidate,
            patch("sessions.tasks.classify_push_task") as classify,
        ):
            await create_push_callback(ref="refs/heads/main").process_callback()

        invalidate.assert_called_once_with("group/repo")
        classify.using.assert_not_called()

    def test_a_tag_push_is_rejected(self):
        assert create_push_callback(ref="refs/tags/v1").accept_callback() is False


def create_issue_note_callback(*, note_id: int, discussion_id: str, labels: list[str] | None = None) -> NoteCallback:
    return NoteCallback(
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
            labels=[Label(title=t) for t in labels or []],
            type="Issue",
        ),
        object_attributes=Note(
            id=note_id,
            action=NoteAction.CREATE,
            noteable_type=NoteableType.ISSUE,
            noteable_id=7,
            discussion_id=discussion_id,
            note="@daiv please look",
            system=False,
        ),
    )


@pytest.mark.parametrize(("label", "use_max"), [("daiv-max", True), ("daiv", False)])
class TestProcessCallback:
    """What each accepted event hands dispatch: the entity, its max mode, who triggered it and the mention."""

    async def test_an_issue_label_dispatches_an_issue_run(self, monkeypatch_dependencies, stub_client, label, use_max):
        callback = create_issue_callback(action=IssueAction.OPEN, issue_labels=[Label(title=label)])

        with patch("webhooks.gitlab.callbacks.dispatch_issue_run", new=AsyncMock()) as dispatch:
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=stub_client,
            repo_id="group/repo",
            issue_iid=42,
            title="Test Issue",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITLAB, uid=10, username="testuser", email="testuser@example.com"),
        )

    async def test_an_issue_mention_dispatches_an_issue_run_for_its_note(
        self, monkeypatch_dependencies, stub_client, label, use_max
    ):
        callback = create_issue_note_callback(note_id=200, discussion_id="d1", labels=[label])

        with patch("webhooks.gitlab.callbacks.dispatch_issue_run", new=AsyncMock()) as dispatch:
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=stub_client,
            repo_id="group/repo",
            issue_iid=7,
            title="Bug",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITLAB, uid=2, username="reviewer", email="reviewer@example.com"),
            mention=Mention(note_id=200, comment_id="d1"),
        )

    async def test_a_merge_request_mention_dispatches_a_comment_run_on_its_source_branch(
        self, monkeypatch_dependencies, stub_client, label, use_max
    ):
        callback = create_note_callback("@daiv please review", labels=[label])

        with patch("webhooks.gitlab.callbacks.dispatch_mr_comment_run", new=AsyncMock()) as dispatch:
            await callback.process_callback()

        dispatch.assert_awaited_once_with(
            client=stub_client,
            repo_id="group/repo",
            merge_request_iid=1,
            ref="feat",
            title="Some MR",
            use_max=use_max,
            author=Author(provider=GitPlatform.GITLAB, uid=2, username="reviewer", email="reviewer@example.com"),
            mention=Mention(note_id=100, comment_id="discussion_1", body="@daiv please review"),
        )

    async def test_a_merge_request_note_without_a_mention_dispatches_as_a_comment(
        self, monkeypatch_dependencies, stub_client, label, use_max
    ):
        callback = create_note_callback("Looks good", labels=[label], note_type="DiffNote")

        with (
            patch("webhooks.gitlab.callbacks.dispatch_mr_comment", new=AsyncMock()) as comment,
            patch("webhooks.gitlab.callbacks.dispatch_mr_comment_run", new=AsyncMock()) as run,
        ):
            await callback.process_callback()

        run.assert_not_awaited()
        comment.assert_awaited_once_with(
            client=stub_client,
            repo_id="group/repo",
            merge_request_iid=1,
            title="Some MR",
            author=Author(provider=GitPlatform.GITLAB, uid=2, username="reviewer", email="reviewer@example.com"),
            comment=Mention(note_id=100, comment_id="discussion_1", body="Looks good", on_diff=True),
        )


@pytest.mark.django_db(transaction=True)
class TestNotesOnAnOwnedMergeRequest:
    @pytest.fixture(autouse=True)
    def inbox(self, monkeypatch_dependencies, stub_client):
        stub_client.git_platform = GitPlatform.GITLAB
        with (
            patch("sessions.inbox.post.drain_logged"),
            patch("webhooks.dispatch.resolve_user", new=AsyncMock(return_value=None)),
        ):
            yield

    async def test_a_diff_mention_reaches_the_owning_session(self):
        ownership = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=1)

        await create_note_callback("@daiv why this line?", note_type="DiffNote").process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.source_key) == ("mention", True, "note:100")
        assert (event.payload["is_diff"], event.payload["discussion_id"]) == (True, "discussion_1")

    async def test_a_diff_mention_on_an_unowned_merge_request_is_still_ignored(self):
        with patch("webhooks.dispatch.address_mr_comments_task") as task:
            await create_note_callback("@daiv why this line?", note_type="DiffNote").process_callback()

        task.get_or_enqueue.assert_not_called()
        assert not await InboxEvent.objects.aexists()

    async def test_a_note_without_a_mention_is_remembered(self):
        ownership = await amake_owned_merge_request(repo_id="group/repo", merge_request_iid=1)

        await create_note_callback("Looks good to me").process_callback()

        event = await InboxEvent.objects.aget(session_id=ownership.session_id)
        assert (event.kind, event.wake, event.payload["body"]) == ("comment", False, "Looks good to me")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
@pytest.mark.parametrize(
    "make_callback",
    [
        pytest.param(lambda note_id: create_issue_note_callback(note_id=note_id, discussion_id="d1"), id="issue"),
        pytest.param(
            lambda note_id: create_note_callback("@daiv please review", note_id=note_id, note_type="DiscussionNote"),
            id="merge_request",
        ),
    ],
)
async def test_a_second_mention_in_a_discussion_runs_after_the_first_succeeded(monkeypatch_dependencies, make_callback):
    """GitLab replies share their discussion's id but dedup on their own note, while dedup also matches finished
    tasks: a reply must still get its own run."""
    from django.tasks import TaskResultStatus

    from django_tasks_db.models import DBTaskResult
    from sessions.models import Run

    await make_callback(200).process_callback()
    await DBTaskResult.objects.aupdate(status=TaskResultStatus.SUCCESSFUL)
    await make_callback(201).process_callback()

    assert await DBTaskResult.objects.acount() == 2
    assert await Run.objects.filter(task_result__isnull=False).acount() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("database_task_backend")
async def test_a_redelivered_note_starts_no_second_run(monkeypatch_dependencies, caplog):
    import logging

    from django.tasks import TaskResultStatus

    from django_tasks_db.models import DBTaskResult
    from sessions.models import Run

    await create_issue_note_callback(note_id=200, discussion_id="d1").process_callback()
    await DBTaskResult.objects.aupdate(status=TaskResultStatus.SUCCESSFUL)
    with caplog.at_level(logging.INFO):
        await create_issue_note_callback(note_id=200, discussion_id="d1").process_callback()

    assert await DBTaskResult.objects.acount() == 1
    assert await Run.objects.acount() == 1
    assert "Skipping duplicate task" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
