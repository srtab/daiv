from unittest.mock import Mock

import pytest

from codebase.clients.base import Emoji
from codebase.clients.gitlab.client import GitLabClient


class TestGitLabClient:
    """Tests for GitLabClient."""

    @pytest.fixture
    def gitlab_client(self):
        """Create a GitLabClient instance with mocked dependencies."""
        from unittest.mock import patch

        mock_gitlab = Mock()
        with patch("codebase.clients.gitlab.client.Gitlab", return_value=mock_gitlab):
            client = GitLabClient(auth_token="test-token", url="https://gitlab.com")  # noqa: S106
            yield client

    def test_has_issue_reaction_returns_true_when_reaction_exists(self, gitlab_client):
        """Test that has_issue_reaction returns True when the current user has awarded the emoji."""
        from codebase.base import User

        mock_project = Mock()
        mock_issue = Mock()
        mock_award_emoji1 = Mock()
        mock_award_emoji2 = Mock()

        # Mock current_user as a cached_property
        type(gitlab_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up award emojis
        mock_award_emoji1.name = "eyes"
        mock_award_emoji1.user = {"id": 456}  # Different user
        mock_award_emoji2.name = "eyes"
        mock_award_emoji2.user = {"id": 123}  # Current user

        gitlab_client.client.projects.get.return_value = mock_project
        mock_project.issues.get.return_value = mock_issue
        mock_issue.awardemojis.list.return_value = [mock_award_emoji1, mock_award_emoji2]

        result = gitlab_client.has_issue_reaction("group/repo", 123, Emoji.EYES)

        assert result is True

    def test_has_issue_reaction_returns_false_when_reaction_not_exists(self, gitlab_client):
        """Test that has_issue_reaction returns False when the current user has not awarded the emoji."""
        from codebase.base import User

        mock_project = Mock()
        mock_issue = Mock()
        mock_award_emoji = Mock()

        # Mock current_user as a cached_property
        type(gitlab_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up award emoji from different user
        mock_award_emoji.name = "eyes"
        mock_award_emoji.user = {"id": 456}

        gitlab_client.client.projects.get.return_value = mock_project
        mock_project.issues.get.return_value = mock_issue
        mock_issue.awardemojis.list.return_value = [mock_award_emoji]

        result = gitlab_client.has_issue_reaction("group/repo", 123, Emoji.EYES)

        assert result is False

    def test_has_issue_reaction_returns_false_when_different_emoji(self, gitlab_client):
        """Test that has_issue_reaction returns False when the current user awarded a different emoji."""
        from codebase.base import User

        mock_project = Mock()
        mock_issue = Mock()
        mock_award_emoji = Mock()

        # Mock current_user as a cached_property
        type(gitlab_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up award emoji with different emoji
        mock_award_emoji.name = "thumbsup"
        mock_award_emoji.user = {"id": 123}  # Current user

        gitlab_client.client.projects.get.return_value = mock_project
        mock_project.issues.get.return_value = mock_issue
        mock_issue.awardemojis.list.return_value = [mock_award_emoji]

        result = gitlab_client.has_issue_reaction("group/repo", 123, Emoji.EYES)

        assert result is False

    def test_has_issue_reaction_returns_false_when_no_reactions(self, gitlab_client):
        """Test that has_issue_reaction returns False when there are no award emojis."""
        from codebase.base import User

        mock_project = Mock()
        mock_issue = Mock()

        # Mock current_user as a cached_property
        type(gitlab_client).current_user = User(id=123, username="daiv", name="DAIV")

        gitlab_client.client.projects.get.return_value = mock_project
        mock_project.issues.get.return_value = mock_issue
        mock_issue.awardemojis.list.return_value = []

        result = gitlab_client.has_issue_reaction("group/repo", 123, Emoji.EYES)

        assert result is False
