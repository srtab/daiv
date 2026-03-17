from unittest.mock import Mock

import pytest

from codebase.clients.github.api.callbacks import IssueCallback, IssueCommentCallback
from codebase.clients.github.api.models import Comment, Issue, Label, Repository, User
from codebase.repo_config import RepositoryConfig


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
    issue_labels: list[dict],
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
        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when a DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[{"name": "daiv"}], label=Label(id=1, name="daiv")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_auto_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-auto label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[{"name": "daiv-auto"}], label=Label(id=2, name="daiv-auto")
        )
        assert callback.accept_callback() is True

    def test_accept_callback_labeled_with_daiv_max_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when daiv-max label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[{"name": "daiv-max"}], label=Label(id=3, name="daiv-max")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_labeled_with_non_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when a non-DAIV label is added."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[{"name": "bug"}], label=Label(id=4, name="bug")
        )
        assert callback.accept_callback() is False

    def test_reject_callback_labeled_without_label_field(self, monkeypatch_dependencies):
        """Test that callback is rejected when labeled action has no label field."""
        callback = create_issue_callback(action="labeled", issue_labels=[{"name": "daiv"}], label=None)
        assert callback.accept_callback() is False

    def test_reject_callback_when_already_reacted(self, monkeypatch_dependencies, mock_repo_client):
        """Test that callback is rejected when DAIV has already reacted to the issue."""
        mock_repo_client.has_issue_reaction.return_value = True

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is False

    def test_reject_callback_closed_issue(self, monkeypatch_dependencies):
        """Test that callback is rejected for closed issues."""
        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}], issue_state="closed")
        assert callback.accept_callback() is False

    def test_reject_callback_edited_action(self, monkeypatch_dependencies):
        """Test that callback is rejected for edited action."""
        callback = create_issue_callback(action="edited", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is False

    def test_reject_callback_issue_addressing_disabled(
        self, monkeypatch_dependencies, mock_repo_config, mock_repo_client
    ):
        """Test that callback is rejected when issue addressing is disabled."""
        mock_repo_config.issue_addressing.enabled = False

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is False

    def test_accept_callback_reopened_with_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is accepted when issue is reopened with a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is True

    def test_reject_callback_reopened_without_daiv_label(self, monkeypatch_dependencies):
        """Test that callback is rejected when issue is reopened without a DAIV label."""
        callback = create_issue_callback(action="reopened", issue_labels=[{"name": "bug"}])
        assert callback.accept_callback() is False

    def test_label_check_case_insensitive(self, monkeypatch_dependencies):
        """Test that label checking is case-insensitive."""
        callback = create_issue_callback(
            action="labeled", issue_labels=[{"name": "DAIV"}], label=Label(id=1, name="DAIV")
        )
        assert callback.accept_callback() is True

    def test_reject_callback_user_not_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is rejected when user is not in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}], sender_username="mallory")
        assert callback.accept_callback() is False

    def test_accept_callback_user_in_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when user is in the allowed usernames list."""
        mock_repo_config.allowed_usernames = ("alice", "bob")

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}], sender_username="alice")
        assert callback.accept_callback() is True

    def test_accept_callback_empty_allowlist(self, monkeypatch_dependencies, mock_repo_config):
        """Test that callback is accepted when allowlist is empty (all users allowed)."""
        mock_repo_config.allowed_usernames = ()

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}])
        assert callback.accept_callback() is True

    def test_allowlist_case_insensitive(self, monkeypatch_dependencies, mock_repo_config):
        """Test that allowlist check is case-insensitive."""
        mock_repo_config.allowed_usernames = ("Alice",)

        callback = create_issue_callback(action="opened", issue_labels=[{"name": "daiv"}], sender_username="alice")
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
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[{"name": "daiv"}]),
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
            issue=Issue(id=100, number=42, title="Test Issue", state="open", labels=[{"name": "daiv"}]),
            comment=Comment(id=200, body="@daiv-bot help", user=User(**{"id": 10, "login": "alice"})),
        )
        assert callback.accept_callback() is True
