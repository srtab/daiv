from unittest.mock import Mock

import pytest

from codebase.clients.github.api.callbacks import IssueCallback, IssueCommentCallback, PullRequestCallback
from codebase.clients.github.api.models import Comment, Issue, Label, PullRequest, Ref, Repository, User
from codebase.repo_config import RepositoryConfig
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL


@pytest.fixture
def mock_repo_client():
    """Mock RepoClient instance."""
    client = Mock()
    client.has_issue_reaction.return_value = False
    return client


@pytest.fixture
def mock_repo_config():
    """Mock RepositoryConfig instance."""
    config = RepositoryConfig()
    config.issue_addressing.enabled = True
    return config


@pytest.fixture
def monkeypatch_dependencies(monkeypatch, mock_repo_client, mock_repo_config):
    """Monkeypatch RepoClient and RepositoryConfig for testing."""
    monkeypatch.setattr("codebase.clients.github.api.callbacks.RepoClient.create_instance", lambda: mock_repo_client)
    monkeypatch.setattr(
        "codebase.clients.github.api.callbacks.RepositoryConfig.get_config", lambda *args, **kwargs: mock_repo_config
    )


def create_issue_callback(
    action: str,
    issue_labels: list[Label],
    issue_state: str = "open",
    label: Label | None = None,
    sender_username: str = "testuser",
) -> IssueCallback:
    """Helper to create an IssueCallback instance."""
    return IssueCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        issue=Issue(id=100, number=42, title="Test Issue", state=issue_state, labels=issue_labels),
        label=label,
        sender=User(**{"id": 10, "login": sender_username}),
    )


class TestIssueCallback:
    """Tests for GitHub IssueCallback."""

    def test_accept_callback_opened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is opened with a DAIV label."""
        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when a DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=1, name=BOT_LABEL)], label=Label(id=1, name="daiv")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_auto_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-auto label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=3, name=BOT_AUTO_LABEL)], label=Label(id=2, name="daiv-auto")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_max_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-max label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=4, name=BOT_MAX_LABEL)], label=Label(id=3, name="daiv-max")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_labeled_with_non_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when a non-DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=2, name="bug")], label=Label(id=4, name="bug")
        )
        assert callback.accept_callback() is False

    def test_reject_callback_labeled_without_label_field(self, monkeypatch_dependencies):
        """Test that callback is rejected when labeled action has no label field."""
        callback = create_issue_callback(action="labeled", issue_labels=[Label(id=1, name=BOT_LABEL)], label=None)
        assert callback.accept_callback() is False

    def test_reject_callback_when_already_reacted(self, monkeypatch_dependencies, mock_repo_client):
        """Test that callback is rejected when DAIV has already reacted to the issue."""
        mock_repo_client.has_issue_reaction.return_value = True

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_reject_callback_closed_issue(self, monkeypatch_dependencies):
        """Test that callback is rejected for closed issues."""
        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], issue_state="closed"
        )
        assert callback.accept_callback() is False

    def test_reject_callback_edited_action(self, monkeypatch_dependencies):
        """Test that callback is rejected for edited action."""
        callback = create_issue_callback(action="edited", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_reject_callback_issue_addressing_disabled(
        self, monkeypatch_dependencies, mock_repo_config, mock_repo_client
    ):
        """Test that callback is rejected when issue addressing is disabled."""
        mock_repo_config.issue_addressing.enabled = False

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is False

    def test_accept_callback_reopened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is reopened with a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_reject_callback_reopened_without_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when issue is reopened without a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[Label(id=2, name="bug")])
        assert callback.accept_callback() is False

    def test_label_check_case_insensitive(self, monkeypatch_dependencies):
        """Test that label checking is case-insensitive."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[Label(id=1, name="DAIV")], label=Label(id=1, name="DAIV")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_user_not_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is rejected when user is not in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="mallory"
        )
        assert callback.accept_callback() is False

    def test_accept_callback_user_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when user is in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="alice"
        )
        assert callback.accept_callback() is True

    def test_accept_callback_empty_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when allowlist is empty (all users allowed)."""
        mock_repo_config.allowed_usernames = ()

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        assert callback.accept_callback() is True

    def test_allowlist_case_insensitive(self, monkeypatch_dependencies, mock_repo_config):
        """Test that allowlist check is case-insensitive."""
        mock_repo_config.allowed_usernames = ("Alice",)

        callback = create_issue_callback(
            action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)], sender_username="alice"
        )
        assert callback.accept_callback() is True


class TestIssueCommentCallbackAllowlist:
    """Tests for GitHub IssueCommentCallback allowlist."""

    def test_reject_comment_user_not_in_allowlist(self, monkeypatch_dependencies, mock_repo_config, mock_repo_client):
        """Test that comment callback is rejected when user is not in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice",)
        mock_repo_client.current_user = User(**{"id": 999, "login": "daiv-bot"})

        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[Label(id=1, name=BOT_LABEL)]),
            comment=Comment(id=200, body="@daiv-bot help", user=User(**{"id": 10, "login": "mallory"})),
        )
        assert callback.accept_callback() is False

    def test_accept_comment_user_in_allowlist(self, monkeypatch_dependencies, mock_repo_config, mock_repo_client):
        """Test that comment callback is accepted when user is in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice",)
        mock_repo_client.current_user = User(**{"id": 999, "login": "daiv-bot"})

        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[Label(id=1, name=BOT_LABEL)]),
            comment=Comment(id=200, body="@daiv-bot help", user=User(**{"id": 10, "login": "alice"})),
        )
        assert callback.accept_callback() is True


def create_pull_request_callback(
    action: str = "closed",
    merged: bool = True,
    labels: list[Label] | None = None,
    user_login: str = "developer",
    base_ref: str = "main",
) -> PullRequestCallback:
    """Helper to create a PullRequestCallback instance."""
    return PullRequestCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        pull_request=PullRequest(
            id=10,
            number=42,
            title="Some PR",
            state="closed" if merged else "open",
            merged=merged,
            merged_at="2026-04-01T10:00:00Z" if merged else None,
            head=Ref(ref="feat/something", sha="abc123"),
            base=Ref(ref=base_ref, sha="def456"),
            labels=labels or [],
            user=User(**{"id": 2, "login": user_login}),
        ),
    )


class TestPullRequestCallback:
    """Tests for GitHub PullRequestCallback."""

    def test_accept_callback_on_closed_and_merged(self):
        """Test that callback is accepted when PR is closed and merged."""
        callback = create_pull_request_callback()
        assert callback.accept_callback() is True

    def test_reject_callback_on_closed_not_merged(self):
        """Test that callback is rejected when PR is closed but not merged."""
        callback = create_pull_request_callback(merged=False)
        assert callback.accept_callback() is False

    def test_reject_callback_on_opened(self):
        """Test that callback is rejected when PR is opened."""
        callback = create_pull_request_callback(action="opened", merged=False)
        assert callback.accept_callback() is False

    def test_reject_callback_on_synchronize(self):
        """Test that callback is rejected when PR is synchronized."""
        callback = create_pull_request_callback(action="synchronize", merged=False)
        assert callback.accept_callback() is False

    def test_reject_callback_when_target_not_default_branch(self):
        """Test that callback is rejected when PR targets a non-default branch."""
        callback = create_pull_request_callback(base_ref="develop")
        assert callback.accept_callback() is False

    async def test_process_callback_enqueues_task(self):
        """Test that process_callback enqueues the merge metrics task with correct args."""
        from unittest.mock import AsyncMock, patch

        callback = create_pull_request_callback()
        with patch("codebase.tasks.record_merge_metrics_task") as mock_task:
            mock_task.aenqueue = AsyncMock()
            await callback.process_callback()

        mock_task.aenqueue.assert_called_once_with(
            repo_id="owner/repo",
            merge_request_iid=42,
            title="Some PR",
            source_branch="feat/something",
            target_branch="main",
            merged_at="2026-04-01T10:00:00Z",
            platform="github",
        )

    async def test_process_callback_coalesces_none_merged_at(self):
        """Test that process_callback passes empty string when merged_at is None."""
        from unittest.mock import AsyncMock, patch

        callback = PullRequestCallback(
            action="closed",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            pull_request=PullRequest(
                id=10,
                number=42,
                title="Some PR",
                state="closed",
                merged=True,
                merged_at=None,
                head=Ref(ref="feat/something", sha="abc123"),
                base=Ref(ref="main", sha="def456"),
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

    async def test_issue_callback_passes_thread_id(self, monkeypatch_dependencies, mock_repo_client):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        callback = create_issue_callback(action="opened", issue_labels=[Label(id=1, name=BOT_LABEL)])
        callback.sender = User(**{"id": 10, "login": "testuser"})
        expected = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)

        with (
            patch("codebase.clients.github.api.callbacks.address_issue_task") as mock_task,
            patch("codebase.clients.github.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.github.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected

    async def test_issue_comment_callback_passes_thread_id(self, monkeypatch_dependencies, mock_repo_config):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        # Plain issue (no pull_request) → ISSUE scope branch.
        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(id=100, number=42, title="Bug", state="open", labels=[]),
            comment=Comment(id=200, body="@daiv help", user=User(**{"id": 10, "login": "alice"})),
        )
        expected = compute_thread_id(repo_slug="owner/repo", scope=Scope.ISSUE, entity_iid=42)

        with (
            patch("codebase.clients.github.api.callbacks.address_issue_task") as mock_task,
            patch("codebase.clients.github.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.github.api.callbacks.note_mentions_daiv", return_value=True),
            patch("codebase.clients.github.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected

    async def test_pr_review_comment_callback_passes_thread_id(self, monkeypatch_dependencies, mock_repo_config):
        from unittest.mock import AsyncMock, patch

        from codebase.base import Scope
        from codebase.utils import compute_thread_id

        mock_repo_config.pull_request_assistant.enabled = True

        # Issue with pull_request set → MR review branch.
        callback = IssueCommentCallback(
            action="created",
            repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
            issue=Issue(
                id=100, number=99, title="PR", state="open", labels=[], pull_request={"url": "https://example/pr/99"}
            ),
            comment=Comment(id=300, body="@daiv review", user=User(**{"id": 10, "login": "alice"})),
        )
        expected = compute_thread_id(repo_slug="owner/repo", scope=Scope.MERGE_REQUEST, entity_iid=99)

        with (
            patch("codebase.clients.github.api.callbacks.address_mr_comments_task") as mock_task,
            patch("codebase.clients.github.api.callbacks.acreate_activity") as mock_activity,
            patch("codebase.clients.github.api.callbacks.note_mentions_daiv", return_value=True),
            patch("codebase.clients.github.api.callbacks.resolve_user", new=AsyncMock(return_value=None)),
        ):
            mock_task.aenqueue = AsyncMock(return_value=type("R", (), {"id": "task-1"})())
            mock_activity.side_effect = AsyncMock(return_value=None)
            await callback.process_callback()

        assert mock_task.aenqueue.call_args.kwargs["thread_id"] == expected
        assert mock_activity.call_args.kwargs["thread_id"] == expected
