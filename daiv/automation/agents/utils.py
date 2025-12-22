import difflib
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from automation.agents.schemas import Image

if TYPE_CHECKING:
    from deepagents.backends.protocol import BACKEND_TYPES


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


def find_original_snippet(snippet: str, file_contents: str, threshold=0.8, initial_line_threshold=0.9) -> list[str]:
    """
    This function finds the original snippet of code in a file given a snippet and the file contents.

    The function first searches for a line in the file that matches the first non-empty line of the snippet
    with a similarity above the initial_line_threshold. It then continues from that point to match the
    rest of the snippet, handling ellipsis cases and using the compute_similarity function to compare
    the accumulated snippet with the file contents.

    Args:
        snippet (str): The snippet of code to find in the file.
        file_contents (str): The contents of the file to search in.
        threshold (float): The similarity threshold for matching the snippet.
        initial_line_threshold (float): The similarity threshold for matching the initial line of the snippet
                                        with a line in the file.

    Returns:
        list[str]: A list of original snippets from the file.
    """
    if snippet.strip() == "":
        return []

    snippet_lines = [line for line in snippet.split("\n") if line.strip()]
    file_lines = file_contents.split("\n")

    # Find the first non-empty line in the snippet
    first_snippet_line = next((line for line in snippet_lines if line.strip()), "")

    all_matches = []

    # Search for a matching initial line in the file
    for start_index, file_line in enumerate(file_lines):
        if compute_similarity(first_snippet_line, file_line) >= initial_line_threshold:
            accumulated_snippet = ""
            snippet_index = 0
            file_index = start_index

            while snippet_index < len(snippet_lines) and file_index < len(file_lines):
                file_line = file_lines[file_index].strip()

                if not file_line:
                    file_index += 1
                    continue

                accumulated_snippet += file_line + "\n"
                similarity = compute_similarity("\n".join(snippet_lines[: snippet_index + 1]), accumulated_snippet)

                if similarity >= threshold:
                    snippet_index += 1

                file_index += 1

            if snippet_index == len(snippet_lines):
                # All lines in the snippet have been matched
                all_matches.append("\n".join(file_lines[start_index:file_index]))

    return all_matches


def compute_similarity(text1: str, text2: str, ignore_whitespace=True) -> float:
    """
    This function computes the similarity between two pieces of text using the difflib.SequenceMatcher class.

    difflib.SequenceMatcher uses the Ratcliff/Obershelp algorithm: it computes the doubled number of matching
    characters divided by the total number of characters in the two strings.

    Args:
        text1 (str): The first piece of text.
        text2 (str): The second piece of text.
        ignore_whitespace (bool): If True, ignores whitespace when comparing the two pieces of text.

    Returns:
        float: The similarity ratio between the two pieces of text.
    """
    if ignore_whitespace:
        text1 = re.sub(r"\s+", "", text1)
        text2 = re.sub(r"\s+", "", text2)

    return difflib.SequenceMatcher(None, text1, text2).ratio()


def get_context_file_content(
    repo_dir: Path, context_file_name: str | None, backend: BACKEND_TYPES | None = None
) -> str | None:
    """
    Get the content of the context file case insensitive.
    If multiple files are found, return the first one.
    If the file is too long, return the first `READ_MAX_LINES` lines.

    Args:
        repo_dir (Path): The directory of the repository.
        context_file_name (str | None): The name of the context file.
        backend (BACKEND_TYPES | None): The backend to use for reading the context file.

    Returns:
        str | None: The content of the context file.
    """
    if not context_file_name:
        return None

    if backend:
        return backend.read(f"/{context_file_name}")

    context_file_path = repo_dir.joinpath(context_file_name)
    if not context_file_path.is_file():
        return None

    return "\n".join(context_file_path.read_text().splitlines()[:500])
