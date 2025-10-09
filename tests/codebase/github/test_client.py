from unittest.mock import Mock, patch

import pytest

from codebase.clients.github import GitHubClient


class TestGitHubClient:
    """Tests for GitHubClient."""

    @pytest.fixture
    def github_client(self):
        """Create a GitHubClient instance with mocked dependencies."""
        with (
            patch("codebase.clients.github.client.GithubIntegration") as mock_integration,
            patch("codebase.clients.github.client.Auth"),
        ):
            mock_installation = Mock()
            mock_github = Mock()
            mock_github.requester.auth.token = "test-token-123"  # noqa: S105

            mock_installation.get_github_for_installation.return_value = mock_github
            mock_integration.return_value.get_app_installation.return_value = mock_installation

            client = GitHubClient(private_key="test-private-key", app_id=12345, installation_id=67890)

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
