"""LangChain v1 middlewares for agents."""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.messages.content import ImageContentBlock, TextContentBlock
from langchain_core.messages.tool import tool_call

from automation.agents.tools.sandbox import bash_tool
from automation.agents.utils import extract_images_from_text
from automation.utils import has_file_changes
from codebase.base import ClientType
from codebase.clients.base import RepoClient
from core.utils import extract_valid_image_mimetype, is_valid_url

if TYPE_CHECKING:
    from langchain.agents.middleware.types import AgentState
    from langgraph.runtime import Runtime

    from automation.agents.schemas import Image
    from codebase.context import RuntimeCtx


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
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[InjectImagesMiddleware()],
        )
        ```
    """

    name = "inject_images_middleware"

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
        extracted_images = await self._images_to_content_blocks(runtime.context.repo_id, extracted_images_data)

        # Return state update that removes old message and adds new one with structured content
        return {
            "messages": [
                RemoveMessage(id=latest_message.id),
                HumanMessage(
                    content_blocks=[TextContentBlock(type="text", text=latest_message.content)] + extracted_images
                ),
            ]
        }

    async def _images_to_content_blocks(self, repo_id: str, images: list[Image]) -> list[ImageContentBlock]:
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
                repo_client.client_slug == ClientType.GITLAB
                and not parsed_url.netloc
                and not parsed_url.scheme
                and parsed_url.path.startswith(("/uploads/", "uploads/"))
            ) or (
                repo_client.client_slug == ClientType.GITHUB
                and parsed_url.netloc
                and parsed_url.scheme
                and (
                    parsed_url.hostname
                    and (
                        parsed_url.hostname.lower() == "github.com"
                        or parsed_url.hostname.lower().endswith(".github.com")
                    )
                )
                and "/user-attachments/" in parsed_url.path
            ):
                if (image_content := await repo_client.get_project_uploaded_file(repo_id, image.url)) and (
                    mime_type := extract_valid_image_mimetype(image_content)
                ):
                    content_blocks.append(
                        ImageContentBlock(
                            type="image", base64=base64.b64encode(image_content).decode(), mime_type=mime_type
                        )
                    )

            # Handle generic valid URLs (external images)
            elif is_valid_url(image.url):
                content_blocks.append(ImageContentBlock(type="image", url=image.url))

        return content_blocks


class FormatCodeMiddleware(AgentMiddleware):
    """
    Middleware to apply format code to the repository to fix the linting issues in the pipeline at the end of the loop.

    The middleware will only apply format code if the:
    - Format code is enabled in the repository configuration.
    - There are file changes made by the executor agent.
    """

    name = "format_code_middleware"

    def __init__(self, *, skip_format_code: bool = False):
        """
        Initialize the format code middleware.

        Args:
            skip_format_code (bool): Whether to skip the format code step.
        """
        super().__init__()
        self.skip_format_code = skip_format_code
        self._tool_call_id = f"tool_call__{uuid.uuid4().hex[:8]}"

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the model call to apply the format code.

        Args:
            state (AgentState): The current agent state containing messages.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, Any] | None: The state updates with the jump to, or None if no format code is needed.
        """
        if (
            not self.skip_format_code
            and state["messages"][-1].type == "tool"
            and state["messages"][-1].tool_call_id == self._tool_call_id
        ):
            return {"jump_to": "end"}
        return None

    @hook_config(can_jump_to=["tools"])
    async def aafter_model(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        After the model call to format the code.

        Args:
            state (AgentState): The current agent state containing messages.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, Any] | None: State updates with new messages, or None if no format code is needed.
        """

        if (
            not self.skip_format_code
            and runtime.context.config.sandbox.enabled
            and runtime.context.config.sandbox.format_code
            and await has_file_changes(runtime.store)
            and (state["messages"][-1].type == "ai" and not state["messages"][-1].tool_calls)
        ):
            return {
                "messages": [
                    RemoveMessage(id=state["messages"][-1].id),
                    AIMessage(
                        content=state["messages"][-1].content,
                        tool_calls=[
                            tool_call(
                                name=bash_tool.name,
                                args={"commands": runtime.context.config.sandbox.format_code},
                                id=self._tool_call_id,
                            )
                        ],
                    ),
                ],
                "jump_to": "tools",
            }

        return None
