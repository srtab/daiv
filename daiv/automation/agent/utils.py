import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langchain_core.messages.content import create_image_block

from automation.agent.schemas import Image
from codebase.base import GitPlatform
from codebase.clients import RepoClient
from core.utils import extract_valid_image_mimetype, is_valid_url

from .conf import settings

if TYPE_CHECKING:
    from langchain_core.messages import ImageContentBlock

    from codebase.repo_config import AgentModelConfig


def extract_images_from_text(text: str) -> list[Image]:
    """
    Extract image URLs from text using regex patterns.

    Supports:
    - Markdown syntax: ![alt text](url)
    - HTML img tags: <img src="url" ...>

    Extracts URLs that either:
    - End with common image extensions (.jpg, .jpeg, .png, .gif, .webp)
    - Are GitHub user-attachments URLs (even without extensions)

    Args:
        text (str): The text content to extract images from.

    Returns:
        list[Image]: List of Image objects with url and filename.
    """
    if not text:
        return []

    images = []
    seen_urls = set()

    # Pattern for markdown images: ![alt text](url)
    markdown_pattern = r"!\[([^\]]*)\]\(([^)]+)\)"
    for match in re.finditer(markdown_pattern, text):
        alt_text = match.group(1).strip()
        url = match.group(2).strip()

        # Only include URLs with valid image extensions
        if _is_valid_image_url(url) and url not in seen_urls:
            filename = _extract_filename(url, alt_text)
            images.append(Image(url=url, filename=filename))
            seen_urls.add(url)

    # Pattern for HTML img tags: <img ... src="url" ... alt="text" ... />
    # This pattern is flexible to handle attributes in any order
    html_pattern = r'<img\s+[^>]*?src=["\'"]([^"\']+)["\'"](.*?)/?>'
    for match in re.finditer(html_pattern, text, re.IGNORECASE):
        url = match.group(1).strip()
        remaining_attrs = match.group(2)

        # Extract alt text if present
        alt_match = re.search(r'alt=["\'"]([^"\'"]*)["\'""]', remaining_attrs, re.IGNORECASE)
        alt_text = alt_match.group(1).strip() if alt_match else ""

        # Only include URLs with valid image extensions
        if _is_valid_image_url(url) and url not in seen_urls:
            filename = _extract_filename(url, alt_text)
            images.append(Image(url=url, filename=filename))
            seen_urls.add(url)

    return images


async def images_to_content_blocks(repo_id: str, images: list[Image]) -> list[ImageContentBlock]:
    """
    Convert a list of images to a list of image content blocks.

    Args:
        repo_id (str): The repository id.
        images (list[Image]): The list of images.

    Returns:
        list[ImageContentBlock]: The list of image content blocks.
    """
    repo_client = RepoClient.create_instance()
    content_blocks = []

    for image in images:
        parsed_url = urlparse(image.url)

        # Handle GitLab relative upload paths and GitHub user-attachments URLs
        if (
            repo_client.git_platform == GitPlatform.GITLAB
            and not parsed_url.netloc
            and not parsed_url.scheme
            and parsed_url.path.startswith(("/uploads/", "uploads/"))
        ) or (
            repo_client.git_platform == GitPlatform.GITHUB
            and parsed_url.netloc
            and parsed_url.scheme
            and (
                parsed_url.hostname
                and (parsed_url.hostname.lower() == "github.com" or parsed_url.hostname.lower().endswith(".github.com"))
            )
            and "/user-attachments/" in parsed_url.path
        ):
            if (image_content := await repo_client.get_project_uploaded_file(repo_id, image.url)) and (
                mime_type := extract_valid_image_mimetype(image_content)
            ):
                content_blocks.append(
                    create_image_block(base64=base64.b64encode(image_content).decode(), mime_type=mime_type)
                )

        # Handle generic valid URLs (external images)
        elif is_valid_url(image.url):
            content_blocks.append(create_image_block(url=image.url))

    return content_blocks


def _is_valid_image_url(url: str) -> bool:
    """
    Check if a URL is a valid image URL.

    Args:
        url (str): The URL to check.

    Returns:
        bool: True if the URL ends with a valid image extension or is a known image hosting service.
    """
    valid_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp")
    # Parse the URL to get the path without query parameters
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Check for valid image extensions
    if any(path.endswith(ext) for ext in valid_extensions):
        return True

    # Check for GitHub user-attachments (always images even without extensions)
    netloc = parsed.netloc.lower()
    is_github_domain = netloc == "github.com" or netloc.endswith(".githubusercontent.com")
    return is_github_domain and "/user-attachments/" in path


def _extract_filename(url: str, alt_text: str = "") -> str:
    """
    Extract filename from URL or use alt text as fallback.

    Args:
        url (str): The image URL.
        alt_text (str): Alternative text from markdown/HTML.

    Returns:
        str: The extracted filename.
    """
    # Try to extract filename from URL path
    parsed = urlparse(url)
    path = Path(parsed.path)

    if path.name:
        return path.name

    # Fallback to alt text if available
    if alt_text:
        return alt_text

    # Last resort: empty string
    return ""


def extract_text_content(content: str | list) -> str:
    """
    Extract text content from a message's content field.

    LangChain messages can have content as either a string or a list of content blocks.
    This function handles both formats and extracts the text.

    Args:
        content: The message content (string or list of content blocks)

    Returns:
        str: The extracted text content
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        # For list-based content (multimodal messages), extract text from text blocks
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                # Handle different block types
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif "text" in block:
                    text_parts.append(block["text"])
            elif isinstance(block, str):
                text_parts.append(block)
        return "".join(text_parts)

    # Fallback for unexpected types
    return str(content)


def get_daiv_agent_kwargs(*, model_config: AgentModelConfig, use_max: bool = False) -> dict[str, Any]:
    """
    Get DAIV agent configuration based on models configuration and use max models configuration.

    Args:
        model_config (DAIVModelConfig): The models configuration.
        use_max (bool): Whether to use the max models configuration.

    Returns:
        dict[str, Any]: Configuration kwargs for DAIVAgent.
    """
    model = model_config.model
    fallback_models = [model_config.fallback_model]
    thinking_level = model_config.thinking_level

    if use_max:
        model = settings.MAX_MODEL_NAME
        fallback_models = [model_config.model, model_config.fallback_model]
        thinking_level = settings.MAX_THINKING_LEVEL

    return {"model_names": [model] + fallback_models, "thinking_level": thinking_level}


def extract_body_from_frontmatter(frontmatter_text: str) -> str:
    """
    Extract prompt from text.

    Args:
        frontmatter_text (str): The frontmatter text to extract content from.

    Returns:
        str: The extracted content.
    """
    frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
    match = re.match(frontmatter_pattern, frontmatter_text, re.DOTALL)
    if not match:
        return frontmatter_text
    return frontmatter_text[match.end() :]
