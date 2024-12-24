from unittest.mock import patch

from automation.agents.image_url_extractor.schemas import Image, ImageTemplate
from codebase.base import ClientType


class TestImageTemplate:
    @patch("automation.agents.image_url_extractor.schemas.is_valid_url")
    def test_from_images_valid_url_only_base64_false(self, mock_is_valid_url):
        mock_is_valid_url.return_value = True
        images = [Image(url="http://example.com/image.png", filename="image.png")]

        result = ImageTemplate.from_images(images, only_base64=False)

        assert len(result) == 1
        assert result[0]["image_url"]["url"] == "http://example.com/image.png"

    @patch("automation.agents.image_url_extractor.schemas.is_valid_url")
    @patch("automation.agents.image_url_extractor.schemas.url_to_data_url")
    def test_from_images_valid_url_only_base64_true(self, mock_url_to_data_url, mock_is_valid_url):
        mock_is_valid_url.return_value = True
        mock_url_to_data_url.return_value = "data:image/png;base64,..."
        images = [Image(url="http://example.com/image.png", filename="image.png")]

        result = ImageTemplate.from_images(images, only_base64=True)

        assert len(result) == 1
        assert result[0]["image_url"]["url"] == "data:image/png;base64,..."

    @patch("automation.agents.image_url_extractor.schemas.build_uri")
    @patch("automation.agents.image_url_extractor.schemas.url_to_data_url")
    @patch("automation.agents.image_url_extractor.schemas.is_valid_url")
    def test_from_images_needs_build_uri(self, mock_is_valid_url, mock_url_to_data_url, mock_build_uri, settings):
        mock_is_valid_url.return_value = False
        mock_build_uri.return_value = "http://gitlab.com/api/v4/projects/1/image.png"
        mock_url_to_data_url.return_value = "data:image/png;base64,..."
        images = [Image(url="uploads/image.png", filename="image.png")]
        with patch("automation.agents.image_url_extractor.schemas.settings", autospec=True) as mock_settings:
            mock_settings.GITLAB_URL = "http://gitlab.com"
            mock_settings.GITLAB_AUTH_TOKEN = "token123"  # noqa: S105

            result = ImageTemplate.from_images(images, repo_client_slug=ClientType.GITLAB, project_id=1)

        assert len(result) == 1
        assert result[0]["image_url"]["url"] == "data:image/png;base64,..."

    @patch("automation.agents.image_url_extractor.schemas.is_valid_url")
    def test_from_images_invalid_gitlab_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        images = [Image(url="invalid_url", filename="")]

        assert len(ImageTemplate.from_images(images, repo_client_slug=ClientType.GITLAB, project_id=1)) == 0
        assert len(ImageTemplate.from_images(images, repo_client_slug=ClientType.GITLAB)) == 0
        assert len(ImageTemplate.from_images(images, project_id=1)) == 0
