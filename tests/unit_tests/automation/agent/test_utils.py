import base64
from unittest.mock import AsyncMock, Mock, patch

from automation.agent.base import ThinkingLevel
from automation.agent.conf import settings
from automation.agent.schemas import Image
from automation.agent.utils import (
    extract_images_from_text,
    extract_text_content,
    get_daiv_agent_kwargs,
    images_to_content_blocks,
)
from codebase.base import GitPlatform
from codebase.repo_config import AgentModelConfig, Models


class TestImagesToContentBlocks:
    @patch("automation.agent.utils.is_valid_url")
    async def test_images_to_content_blocks_valid_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = True
        images = [Image(url="http://example.com/image.png", filename="image.png")]

        result = await images_to_content_blocks("repo_id", images)

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "url" in result[0] and result[0]["url"] == "http://example.com/image.png"
        assert "mime_type" not in result[0]

    @patch("automation.agent.utils.is_valid_url")
    @patch("automation.agent.utils.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_images_to_content_blocks_needs_build_uri(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        result = await images_to_content_blocks("repo_id", [Image(url="uploads/image.png", filename="image.png")])

        assert len(result) == 1
        assert "type" in result[0] and result[0]["type"] == "image"
        assert "base64" in result[0] and result[0]["base64"] == base64.b64encode(b"image content").decode()
        assert "mime_type" in result[0] and result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

    @patch("automation.agent.utils.is_valid_url")
    async def test_from_images_invalid_gitlab_url(self, mock_is_valid_url):
        mock_is_valid_url.return_value = False
        images = [Image(url="invalid_url", filename="")]

        assert len(await images_to_content_blocks("repo_id", images)) == 0

    @patch("automation.agent.utils.extract_valid_image_mimetype", new=Mock(return_value="image/png"))
    async def test_from_images_github_user_attachments(self, mock_repo_client):
        """Test that GitHub user-attachments URLs are downloaded with authentication."""
        mock_repo_client.git_platform = GitPlatform.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=b"github image content")

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await images_to_content_blocks("repo_id", images)

        assert len(result) == 1
        assert result[0]["type"] == "image"
        assert result[0]["base64"] == base64.b64encode(b"github image content").decode()
        assert result[0]["mime_type"] == "image/png"
        assert "url" not in result[0]

        # Verify the download was called with the correct URL
        mock_repo_client.get_project_uploaded_file.assert_called_once()
        call_args = mock_repo_client.get_project_uploaded_file.call_args
        assert call_args[0][1] == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"

    async def test_images_to_content_blocks_github_user_attachments_download_fails(self, mock_repo_client):
        """Test that failed GitHub downloads don't add to result."""
        mock_repo_client.git_platform = GitPlatform.GITHUB
        mock_repo_client.get_project_uploaded_file = AsyncMock(return_value=None)

        images = [
            Image(
                url="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e",
                filename="5005705d-f605-4dd7-b56c-eb513201b40e",
            )
        ]

        result = await images_to_content_blocks("repo_id", images)

        assert len(result) == 0

    async def test_images_to_content_blocks_github_external_url_not_user_attachments(self, mock_repo_client):
        """Test that non-user-attachments GitHub URLs are treated as regular URLs."""
        mock_repo_client.git_platform = GitPlatform.GITHUB

        with patch("automation.agent.utils.is_valid_url", return_value=True):
            images = [Image(url="https://github.com/user/repo/raw/main/image.png", filename="image.png")]

            result = await images_to_content_blocks("repo_id", images)

            assert len(result) == 1
            assert "url" in result[0] and result[0]["url"] == "https://github.com/user/repo/raw/main/image.png"


class TestGetDaivAgentKwargs:
    """Test the get_daiv_agent_kwargs() function."""

    def test_get_daiv_agent_kwargs_without_use_max(self):
        """Test that get_daiv_agent_kwargs returns default config when use_max=False."""
        models_config = Models()
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=False)

        assert kwargs["model_names"] == [settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME]
        assert kwargs["thinking_level"] == settings.THINKING_LEVEL

    def test_get_daiv_agent_kwargs_with_use_max(self):
        """Test that get_daiv_agent_kwargs sets high-performance mode when use_max=True."""
        models_config = Models()
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=True)

        # When use_max=True, the fallback is the regular planning_model from config
        assert kwargs["model_names"] == [settings.MAX_MODEL_NAME, settings.MODEL_NAME, settings.FALLBACK_MODEL_NAME]
        # When use_max=True, the fallback is the regular execution_model from config
        assert kwargs["thinking_level"] == settings.MAX_THINKING_LEVEL

    def test_get_daiv_agent_kwargs_does_not_include_skip_approval(self):
        """Test that get_daiv_agent_kwargs does not set skip_approval."""
        models_config = Models()
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=False)

        # Note: skip_approval is not in kwargs as it's handled elsewhere
        assert "skip_approval" not in kwargs

    def test_get_daiv_agent_kwargs_with_yaml_model_config(self):
        """Test that get_daiv_agent_kwargs uses YAML model config when available."""
        # Set up YAML model config
        model_config = AgentModelConfig(
            model="openrouter:anthropic/claude-haiku-4.5",
            fallback_model="openrouter:openai/gpt-4.1-mini",
            thinking_level="low",
        )
        models_config = Models(agent=model_config)
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=False)

        assert kwargs["model_names"] == ["openrouter:anthropic/claude-haiku-4.5", "openrouter:openai/gpt-4.1-mini"]
        assert kwargs["thinking_level"] == ThinkingLevel.LOW

    def test_get_daiv_agent_kwargs_use_max_overrides_yaml_config(self):
        """Test that use_max=True overrides YAML config."""
        # Set up YAML model config
        model_config = AgentModelConfig(model="openrouter:anthropic/claude-haiku-4.5", thinking_level="low")
        models_config = Models(agent=model_config)
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=True)

        # use_max should override YAML config
        assert kwargs["model_names"][0] == settings.MAX_MODEL_NAME
        assert kwargs["thinking_level"] == settings.MAX_THINKING_LEVEL

    def test_get_daiv_agent_kwargs_partial_yaml_config(self):
        """Test that partial YAML config merges with environment defaults."""
        # Set up partial YAML model config (only model)
        model_config = AgentModelConfig(model="openrouter:anthropic/claude-haiku-4.5")
        models_config = Models(agent=model_config)
        kwargs = get_daiv_agent_kwargs(model_config=models_config.agent, use_max=False)

        # model should come from YAML
        assert kwargs["model_names"][0] == "openrouter:anthropic/claude-haiku-4.5"
        # fallback_model should come from env vars
        assert kwargs["model_names"][1] == settings.FALLBACK_MODEL_NAME


# Tests for extract_images_from_text


def test_extract_images_from_text_markdown_simple():
    text = "Here is an image: ![Performance Metrics](https://example.com/performance_metrics.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/performance_metrics.png"
    assert images[0].filename == "performance_metrics.png"


def test_extract_images_from_text_markdown_multiple():
    text = """
    ![Image 1](https://example.com/image1.jpg)
    Some text here
    ![Image 2](https://example.com/image2.png)
    """
    images = extract_images_from_text(text)
    assert len(images) == 2
    assert images[0].url == "https://example.com/image1.jpg"
    assert images[0].filename == "image1.jpg"
    assert images[1].url == "https://example.com/image2.png"
    assert images[1].filename == "image2.png"


def test_extract_images_from_text_html_simple():
    text = '<img src="https://example.com/auth_error_screenshot.jpg" alt="Authentication Error Screenshot">'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/auth_error_screenshot.jpg"
    assert images[0].filename == "auth_error_screenshot.jpg"


def test_extract_images_from_text_html_without_alt():
    text = '<img src="https://example.com/screenshot.png">'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/screenshot.png"
    assert images[0].filename == "screenshot.png"


def test_extract_images_from_text_html_complex_attributes():
    text = '<img width="1024" height="247" alt="Image" src="https://example.com/image.png" />'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png"
    assert images[0].filename == "image.png"


def test_extract_images_from_text_mixed_markdown_and_html():
    text = """
    ![Markdown Image](https://example.com/markdown.jpg)
    <img src="https://example.com/html.png" alt="HTML Image">
    """
    images = extract_images_from_text(text)
    assert len(images) == 2
    assert images[0].url == "https://example.com/markdown.jpg"
    assert images[0].filename == "markdown.jpg"
    assert images[1].url == "https://example.com/html.png"
    assert images[1].filename == "html.png"


def test_extract_images_from_text_with_query_parameters():
    text = "![Image](https://example.com/image.png?size=large&format=png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png?size=large&format=png"
    assert images[0].filename == "image.png"


def test_extract_images_from_text_relative_urls():
    text = "![Upload](/uploads/df8467e2dffb12ae2ca9d5f1db15cad3/screenshot.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "/uploads/df8467e2dffb12ae2ca9d5f1db15cad3/screenshot.png"
    assert images[0].filename == "screenshot.png"


def test_extract_images_from_text_no_images():
    text = "This is just regular text with no images."
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_empty_string():
    images = extract_images_from_text("")
    assert len(images) == 0


def test_extract_images_from_text_none():
    images = extract_images_from_text(None)
    assert len(images) == 0


def test_extract_images_from_text_without_valid_extensions():
    text = """
    ![No Extension](https://example.com/image)
    <img src="https://example.com/file.txt">
    """
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_github_user_attachments_without_extension():
    # GitHub user-attachments URLs should be extracted even without explicit extensions
    text = '<img src="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e" />'
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"
    assert images[0].filename == "5005705d-f605-4dd7-b56c-eb513201b40e"


def test_extract_images_from_text_duplicate_urls():
    # Should not return duplicates
    text = """
    ![Image 1](https://example.com/image.png)
    ![Image 2](https://example.com/image.png)
    """
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://example.com/image.png"


def test_extract_images_from_text_all_extensions():
    text = """
    ![JPG](https://example.com/image.jpg)
    ![JPEG](https://example.com/image.jpeg)
    ![PNG](https://example.com/image.png)
    ![GIF](https://example.com/image.gif)
    ![WEBP](https://example.com/image.webp)
    """
    images = extract_images_from_text(text)
    assert len(images) == 5
    assert images[0].url == "https://example.com/image.jpg"
    assert images[1].url == "https://example.com/image.jpeg"
    assert images[2].url == "https://example.com/image.png"
    assert images[3].url == "https://example.com/image.gif"
    assert images[4].url == "https://example.com/image.webp"


def test_extract_images_from_text_filename_from_alt_text_when_no_filename_in_url():
    # When URL has no filename, alt text should be used
    text = "![My Custom Name](https://example.com/path/to/image.png)"
    images = extract_images_from_text(text)
    assert len(images) == 1
    # Filename should come from URL path, not alt text
    assert images[0].filename == "image.png"


def test_extract_images_from_text_case_insensitive_extensions():
    text = """
    ![Upper](https://example.com/image.PNG)
    ![Lower](https://example.com/image.jpg)
    """
    images = extract_images_from_text(text)
    assert len(images) == 2


def test_extract_images_from_text_github_user_attachments_with_attributes():
    # GitHub user-attachments with multiple attributes
    text = (
        '<img width="1024" height="247" alt="Image" '
        'src="https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e" />'
    )
    images = extract_images_from_text(text)
    assert len(images) == 1
    assert images[0].url == "https://github.com/user-attachments/assets/5005705d-f605-4dd7-b56c-eb513201b40e"
    assert images[0].filename == "5005705d-f605-4dd7-b56c-eb513201b40e"


def test_extract_images_from_text_non_github_urls_without_extensions():
    # Non-GitHub URLs without extensions should still be skipped
    text = """
    ![No Extension](https://example.com/image)
    <img src="https://example.com/assets/abc123">
    """
    images = extract_images_from_text(text)
    assert len(images) == 0


def test_extract_images_from_text_github_user_attachments_security_malicious_domains():
    # Test that malicious domains with 'github.com' substring are rejected
    text = """
    ![Evil 1](https://evil-github.com/user-attachments/assets/abc123)
    ![Evil 2](https://github.com.attacker.com/user-attachments/assets/abc123)
    ![Evil 3](https://fakegithub.com/user-attachments/assets/abc123)
    ![Evil 4](https://notgithub.com/user-attachments/assets/abc123)
    """
    images = extract_images_from_text(text)
    assert len(images) == 0, "Malicious domains should be rejected"


def test_extract_images_from_text_github_user_attachments_legitimate_subdomains():
    # Test that legitimate GitHub subdomains are accepted
    text = """
    ![Valid 1](https://github.com/user-attachments/assets/abc123)
    ![Valid 2](https://private-user-images.githubusercontent.com/123/456/image.png)
    ![Valid 3](https://user-images.githubusercontent.com/user-attachments/assets/def456)
    """
    images = extract_images_from_text(text)
    # All legitimate GitHub domains should be accepted
    assert len(images) == 3
    assert images[0].url == "https://github.com/user-attachments/assets/abc123"
    assert images[1].url == "https://private-user-images.githubusercontent.com/123/456/image.png"
    assert images[2].url == "https://user-images.githubusercontent.com/user-attachments/assets/def456"


# Tests for extract_text_content


def test_extract_text_content_from_string():
    """Test extracting text from string content."""
    content = "This is a simple text message"
    result = extract_text_content(content)
    assert result == "This is a simple text message"
    assert isinstance(result, str)


def test_extract_text_content_from_empty_string():
    """Test extracting text from empty string."""
    content = ""
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_text_blocks():
    """Test extracting text from list of content blocks with type field."""
    content = [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world!"}]
    result = extract_text_content(content)
    assert result == "Hello world!"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_mixed_blocks():
    """Test extracting text from list with mixed block types."""
    content = [
        {"type": "text", "text": "Text part"},
        {"type": "image", "url": "http://example.com/image.png"},
        {"type": "text", "text": " continues here"},
    ]
    result = extract_text_content(content)
    assert result == "Text part continues here"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_text_field_only():
    """Test extracting text from list of blocks with only text field."""
    content = [{"text": "First part"}, {"text": " second part"}]
    result = extract_text_content(content)
    assert result == "First part second part"
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_string_items():
    """Test extracting text from list of plain strings."""
    content = ["Hello ", "world", "!"]
    result = extract_text_content(content)
    assert result == "Hello world!"
    assert isinstance(result, str)


def test_extract_text_content_from_empty_list():
    """Test extracting text from empty list."""
    content = []
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_list_with_no_text():
    """Test extracting text from list with no text content."""
    content = [
        {"type": "image", "url": "http://example.com/image.png"},
        {"type": "audio", "url": "http://example.com/audio.mp3"},
    ]
    result = extract_text_content(content)
    assert result == ""
    assert isinstance(result, str)


def test_extract_text_content_from_complex_structure():
    """Test extracting text from complex multimodal content structure."""
    # This simulates the structure that was causing the bug
    content = [
        {"id": "rs_050509c9a1c76d8900690cbf77cf5481a0aed3d5a2228f6dd0", "type": "text", "text": "Sure, "},
        {"type": "text", "text": "I can help with that."},
    ]
    result = extract_text_content(content)
    assert result == "Sure, I can help with that."
    assert isinstance(result, str)


def test_extract_text_content_handles_unexpected_types_gracefully():
    """Test that unexpected content types are converted to string."""
    content = 123
    result = extract_text_content(content)
    assert result == "123"
    assert isinstance(result, str)


def test_extract_text_content_from_none():
    """Test extracting text from None value."""
    content = None
    result = extract_text_content(content)
    assert result == "None"
    assert isinstance(result, str)
