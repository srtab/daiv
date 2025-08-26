import base64
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from automation.agents.schemas import Image, ImageTemplate


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

    @patch("automation.agents.schemas.build_uri")
    @patch("automation.agents.schemas.async_download_url", new_callable=AsyncMock, return_value=b"image content")
    @patch("automation.agents.schemas.is_valid_url")
    async def test_from_images_needs_build_uri(
        self, mock_is_valid_url, mock_async_download_url, mock_build_uri, settings
    ):
        mock_is_valid_url.return_value = False
        mock_build_uri.return_value = "http://gitlab.com/api/v4/projects/1/image.png"
        images = [Image(url="uploads/image.png", filename="image.png")]
        with patch("automation.agents.schemas.settings", autospec=True) as mock_settings:
            mock_settings.GITLAB_URL = "http://gitlab.com"
            mock_settings.GITLAB_AUTH_TOKEN = SecretStr("token123")  # noqa: S105

            result = await ImageTemplate.from_images(images)

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
