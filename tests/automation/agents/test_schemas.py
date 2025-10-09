import base64
from unittest.mock import AsyncMock, Mock, patch

from automation.agents.schemas import Image, ImageTemplate
from codebase.base import ClientType


@patch("automation.agents.schemas.get_repository_ctx", new=Mock())
class TestImageTemplate:
    @patch("automation.agents.schemas.is_valid_url")
    async def test_from_images_valid_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = True
        images = [Image(url="http://example.com/image.png", filename="image.png")]

        result = await ImageTemplate.from_images(images)

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "source_type" in result[0] and result[0]["source_type"] == "url"
        assert "url" in result[0] and result[0]["url"] == "http://example.com/image.png"
        assert "data" not in result[0]
        assert "mime_type" not in result[0]

    @patch("automation.agents.schemas.is_valid_url")
    @patch("automation.agents.schemas.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_from_images_needs_build_uri(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        result = await ImageTemplate.from_images([Image(url="uploads/image.png", filename="image.png")])

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "source_type" in result[0] and result[0]["source_type"] == "base64"
        assert "data" in result[0] and result[0]["data"] == base64.b64encode(b"image content").decode()
        assert "mime_type" in result[0] and result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

    @patch("automation.agents.schemas.is_valid_url")
    async def test_from_images_invalid_gitlab_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        images = [Image(url="invalid_url", filename="")]

        assert len(await ImageTemplate.from_images(images)) == 0

    @patch("automation.agents.schemas.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_from_images_github_user_attachments(self, mock_repo_client):
        """Test that GitHub user-attachments URLs are downloaded with authentication."""
        mock_repo_client.client_slug = ClientType.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=b"github image content")

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await ImageTemplate.from_images(images)

        assert len(result) == 1
        assert result[0]["type"] == "image"
        assert result[0]["source_type"] == "base64"
        assert result[0]["data"] == base64.b64encode(b"github image content").decode()
        assert result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

        # Verify the download was called with the correct URL
        mock_repo_client.get_project_uploaded_file.assert_called_once()
        call_args = mock_repo_client.get_project_uploaded_file.call_args
        assert call_args[0][1] == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"

    async def test_from_images_github_user_attachments_download_fails(self, mock_repo_client):
        """Test that failed GitHub downloads don't add to result."""
        mock_repo_client.client_slug = ClientType.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=None)

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await ImageTemplate.from_images(images)

        assert len(result) == 0

    async def test_from_images_github_external_url_not_user_attachments(self, mock_repo_client):
        """Test that non-user-attachments GitHub URLs are treated as regular URLs."""
        mock_repo_client.client_slug = ClientType.GITHUB

        with patch("automation.agents.schemas.is_valid_url", return_value=True):
            images = [Image(url="https://github.com/user/repo/raw/main/image.png", filename="image.png")]

            result = await ImageTemplate.from_images(images)

            assert len(result) == 1
            assert result[0]["source_type"] == "url"
            assert result[0]["url"] == "https://github.com/user/repo/raw/main/image.png"
