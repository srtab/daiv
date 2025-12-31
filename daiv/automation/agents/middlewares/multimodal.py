from __future__ import annotations

import base64
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.messages.content import ImageContentBlock, create_image_block, create_text_block

from automation.agents.utils import extract_images_from_text
from codebase.base import GitPlatform
from codebase.clients.base import RepoClient
from core.utils import extract_valid_image_mimetype, is_valid_url

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState
    from langgraph.runtime import Runtime

    from automation.agents.schemas import Image
    from codebase.context import RuntimeCtx


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


class InjectImagesMiddleware(AgentMiddleware):
    """
    Extract images from the last message and inject them into the message content.

    This middleware extracts images from markdown or HTML in the last message,
    downloads/processes them, and reformats the message to include structured
    image content that can be understood by multimodal models.

    Supports:
    - Markdown syntax: ![alt text](url)
    - HTML img tags: <img src="url" ...>

    The middleware:
    1. Extracts image URLs from the last message
    2. Converts them to proper image content blocks (base64 or URL)
    3. Removes the original message and adds a new one with image content blocks

    Example:
        ```python
        from langchain.chat_models import init_chat_model
        from langchain.agents import create_agent

        model = init_chat_model(
            model="openai:gpt-5",
        )

        agent = create_agent(
            model=model,
            middleware=[InjectImagesMiddleware()],
        )
        ```
    """

    async def abefore_agent(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Before the agent start the execution loop.

        Args:
            state (AgentState): The current agent state containing messages.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, list] | None: State updates with new messages, or None if no images found.
        """
        if not state["messages"]:
            return None

        latest_message = state["messages"][-1]

        # Only process human messages with text content
        if (
            not hasattr(latest_message, "content")
            or latest_message.type != "human"
            or not isinstance(latest_message.content, str)
        ):
            return None

        extracted_images_data = extract_images_from_text(latest_message.content)

        if not extracted_images_data:
            return None

        # Convert images to proper content blocks
        extracted_images = await images_to_content_blocks(runtime.context.repo_id, extracted_images_data)

        if not extracted_images:
            return None

        # Return state update that removes old message and adds new one with structured content
        return {
            "messages": [
                RemoveMessage(id=latest_message.id),
                HumanMessage(content_blocks=[create_text_block(text=latest_message.content)] + extracted_images),
            ]
        }
