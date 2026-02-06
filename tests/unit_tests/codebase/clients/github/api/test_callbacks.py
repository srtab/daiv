from unittest.mock import Mock

import pytest

from codebase.clients.github.api.callbacks import IssueCallback
from codebase.clients.github.api.models import Issue, Label, Repository
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
    action: str, issue_labels: list[dict], issue_state: str = "open", label: Label | None = None
) -> IssueCallback:
    """Helper to create an IssueCallback instance."""
    return IssueCallback(
        action=action,
        repository=Repository(id=1, full_name="owner/repo", default_branch="main"),
        issue=Issue(id=100, number=42, title="Test Issue", state=issue_state, labels=issue_labels),
        label=label,
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
