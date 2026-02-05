from unittest.mock import Mock, patch

import pytest
from github import UnknownObjectException
from github.IssueComment import IssueComment
from github.PullRequestComment import PullRequestComment

from codebase.clients.base import Emoji
from codebase.clients.github.client import GitHubClient


class TestGitHubClient:
    """Tests for GitHubClient."""

    @pytest.fixture
    def github_client(self):
        """Create a GitHubClient instance with mocked dependencies."""
        integration = Mock()
        mock_installation = Mock()
        mock_github = Mock()
        mock_github.requester.auth.token = "test-token-123"  # noqa: S105

        mock_installation.get_github_for_installation.return_value = mock_github
        integration.get_app_installation.return_value = mock_installation

        client = GitHubClient(integration=integration, installation_id=67890)
        yield client

    @patch("codebase.clients.github.client.async_download_url")
    async def test_get_project_uploaded_file_success(self, mock_download, github_client):
        """Test successful download of GitHub user-attachments file."""
        mock_download.return_value = b"image content"

        url = "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e.png"
        result = await github_client.get_project_uploaded_file("owner/repo", url)

        assert result == b"image content"
        mock_download.assert_called_once_with(url, headers={"Authorization": "Bearer test-token-123"})

    @patch("codebase.clients.github.client.async_download_url")
    async def test_get_project_uploaded_file_failure(self, mock_download, github_client):
        """Test failed download returns None."""
        mock_download.return_value = None

        url = "https://github.com/user-attachments/assets/invalid.png"
        result = await github_client.get_project_uploaded_file("owner/repo", url)

        assert result is None
        mock_download.assert_called_once_with(url, headers={"Authorization": "Bearer test-token-123"})

    @patch("codebase.clients.github.client.async_download_url")
    async def test_get_project_uploaded_file_uses_bearer_token(self, mock_download, github_client):
        """Test that the method uses Bearer token authentication."""
        mock_download.return_value = b"content"

        url = "https://github.com/user-attachments/assets/test.jpg"
        await github_client.get_project_uploaded_file("owner/repo", url)

        # Verify the Authorization header format
        call_args = mock_download.call_args
        assert call_args[1]["headers"]["Authorization"] == "Bearer test-token-123"

    def test_create_issue_emoji_converts_note_id_to_int(self, github_client):
        """Test that create_issue_emoji converts string note_id to int."""
        mock_repo = Mock()
        mock_issue = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_issue_emoji("owner/repo", 123, Emoji.THUMBSUP, "3645723306")

        # Verify that get_comment was called with an integer
        mock_issue.get_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    def test_has_issue_reaction_returns_true_when_reaction_exists(self, github_client):
        """Test that has_issue_reaction returns True when the current user has reacted with the specified emoji."""
        from codebase.base import User

        mock_repo = Mock()
        mock_issue = Mock()
        mock_reaction1 = Mock()
        mock_reaction2 = Mock()
        mock_user1 = Mock()
        mock_user2 = Mock()

        # Mock current_user as a cached_property
        type(github_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up reactions
        mock_user1.id = 456  # Different user
        mock_user2.id = 123  # Current user
        mock_reaction1.content = "eyes"
        mock_reaction1.user = mock_user1
        mock_reaction2.content = "eyes"
        mock_reaction2.user = mock_user2

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_reactions.return_value = [mock_reaction1, mock_reaction2]

        result = github_client.has_issue_reaction("owner/repo", 123, Emoji.EYES)

        assert result is True

    def test_has_issue_reaction_returns_false_when_reaction_not_exists(self, github_client):
        """Test that has_issue_reaction returns False when the current user has not reacted."""
        from codebase.base import User

        mock_repo = Mock()
        mock_issue = Mock()
        mock_reaction = Mock()
        mock_user = Mock()

        # Mock current_user as a cached_property
        type(github_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up reaction from different user
        mock_user.id = 456
        mock_reaction.content = "eyes"
        mock_reaction.user = mock_user

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_reactions.return_value = [mock_reaction]

        result = github_client.has_issue_reaction("owner/repo", 123, Emoji.EYES)

        assert result is False

    def test_has_issue_reaction_returns_false_when_different_emoji(self, github_client):
        """Test that has_issue_reaction returns False when the current user reacted with a different emoji."""
        from codebase.base import User

        mock_repo = Mock()
        mock_issue = Mock()
        mock_reaction = Mock()
        mock_user = Mock()

        # Mock current_user as a cached_property
        type(github_client).current_user = User(id=123, username="daiv", name="DAIV")

        # Set up reaction with different emoji
        mock_user.id = 123  # Current user
        mock_reaction.content = "+1"  # Different emoji
        mock_reaction.user = mock_user

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_reactions.return_value = [mock_reaction]

        result = github_client.has_issue_reaction("owner/repo", 123, Emoji.EYES)

        assert result is False

    def test_has_issue_reaction_returns_false_when_no_reactions(self, github_client):
        """Test that has_issue_reaction returns False when there are no reactions."""
        from codebase.base import User

        mock_repo = Mock()
        mock_issue = Mock()

        # Mock current_user as a cached_property
        type(github_client).current_user = User(id=123, username="daiv", name="DAIV")

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_issue.return_value = mock_issue
        mock_issue.get_reactions.return_value = []

        result = github_client.has_issue_reaction("owner/repo", 123, Emoji.EYES)

        assert result is False

    def test_create_merge_request_note_emoji_review_comment(self, github_client):
        """Test that create_merge_request_note_emoji converts string note_id to int for review comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        mock_pr.get_review_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_merge_request_note_emoji("owner/repo", 712, Emoji.THUMBSUP, "3645723306")

        # Verify that get_review_comment was called with an integer
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    def test_create_merge_request_note_emoji_issue_comment_fallback(self, github_client):
        """Test that create_merge_request_note_emoji falls back to issue comment when review comment not found."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_comment = Mock()

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        # Simulate review comment not found
        mock_pr.get_review_comment.side_effect = UnknownObjectException(404, {}, {})
        mock_pr.get_issue_comment.return_value = mock_comment

        # Pass note_id as a string
        github_client.create_merge_request_note_emoji("owner/repo", 712, Emoji.THUMBSUP, "3645723306")

        # Verify that both methods were called with an integer
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        mock_comment.create_reaction.assert_called_once_with("+1")

    def test_get_merge_request_comment_converts_comment_id_to_int_issue_comment(self, github_client):
        """Test that get_merge_request_comment converts string comment_id to int for issue comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.login = "testuser"
        mock_user.name = "Test User"

        mock_comment = Mock(spec=IssueComment)
        mock_comment.id = 3645723306
        mock_comment.body = "Test comment"
        mock_comment.user = mock_user

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        mock_pr.get_issue_comment.return_value = mock_comment

        # Pass comment_id as a string
        result = github_client.get_merge_request_comment("owner/repo", 712, "3645723306")

        # Verify that get_issue_comment was called with an integer
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        assert result.id == "3645723306"
        assert len(result.notes) == 1

    def test_get_merge_request_comment_converts_comment_id_to_int_review_comment(self, github_client):
        """Test that get_merge_request_comment converts string comment_id to int for review comments."""
        mock_repo = Mock()
        mock_pr = Mock()
        mock_user = Mock()
        mock_user.id = 1
        mock_user.login = "testuser"
        mock_user.name = "Test User"

        mock_comment = Mock(spec=PullRequestComment)
        mock_comment.id = 3645723306
        mock_comment.body = "Test review comment"
        mock_comment.user = mock_user
        mock_comment.path = "test.py"
        mock_comment.commit_id = "abc123"
        mock_comment.line = 10
        mock_comment.start_line = None
        mock_comment.side = "RIGHT"
        mock_comment.start_side = None
        mock_comment.subject_type = "line"

        github_client.client.get_repo.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr
        # Simulate issue comment not found
        mock_pr.get_issue_comment.side_effect = UnknownObjectException(404, {}, {})
        mock_pr.get_review_comment.return_value = mock_comment

        # Pass comment_id as a string
        result = github_client.get_merge_request_comment("owner/repo", 712, "3645723306")

        # Verify that both methods were called with an integer
        mock_pr.get_issue_comment.assert_called_once_with(3645723306)
        mock_pr.get_review_comment.assert_called_once_with(3645723306)
        assert result.id == "3645723306"
        assert len(result.notes) == 1
