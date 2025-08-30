import base64
from unittest.mock import Mock, patch

from automation.agents.schemas import Image, ImageTemplate


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
    async def test_from_images_needs_build_uri(self, mock_is_valid_url, settings):
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
