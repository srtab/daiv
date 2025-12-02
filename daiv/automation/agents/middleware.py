from __future__ import annotations

import base64
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_anthropic.middleware.prompt_caching import (
    AnthropicPromptCachingMiddleware as AnthropicPromptCachingMiddlewareV0,
)
from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.messages.content import ImageContentBlock, create_image_block, create_text_block
from langchain_openai.chat_models import ChatOpenAI
from langgraph.types import Overwrite

from automation.agents.base import ModelProvider
from automation.agents.tools.navigation import READ_MAX_LINES
from automation.agents.utils import extract_images_from_text
from codebase.base import ClientType
from codebase.clients.base import RepoClient
from core.utils import extract_valid_image_mimetype, is_valid_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import AgentState, ModelCallResult
    from langchain_core.language_models import BaseChatModel
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
        from langchain.chat_models import init_chat_model
        from langchain.agents import create_agent

        model = init_chat_model(
            model="openai:gpt-5",
        )

        agent = create_agent(
            model=model,
            middleware=[InjectImagesMiddleware(image_inputs_supported=model.profile.get("image_inputs", True))],
        )
        ```
    """

    name = "inject_images_middleware"

    def __init__(self, *, image_inputs_supported: bool = True):
        """
        Initialize the middleware.
        """
        super().__init__()
        self.image_inputs_supported = image_inputs_supported

    async def abefore_agent(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, list] | None:
        """
        Before the agent start the execution loop.

        Args:
            state (AgentState): The current agent state containing messages.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, list] | None: State updates with new messages, or None if no images found.
        """
        if not state["messages"] or not self.image_inputs_supported:
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

        if not extracted_images:
            return None

        # Return state update that removes old message and adds new one with structured content
        return {
            "messages": [
                RemoveMessage(id=latest_message.id),
                HumanMessage(content_blocks=[create_text_block(text=latest_message.content)] + extracted_images),
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
                        create_image_block(base64=base64.b64encode(image_content).decode(), mime_type=mime_type)
                    )

            # Handle generic valid URLs (external images)
            elif is_valid_url(image.url):
                content_blocks.append(create_image_block(url=image.url))

        return content_blocks


class AgentsMDMiddleware(AgentMiddleware):
    """
    Middleware to inject the agents instructions from the AGENTS.md file into the agent state.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[AgentsMDMiddleware()],
            context_schema=RuntimeCtx,
        )
        ```
    """

    name = "agents_md_middleware"

    async def abefore_agent(self, state: AgentState, runtime: Runtime[RuntimeCtx]) -> dict[str, Any] | None:
        """
        Before the agent starts, inject the agents instructions from the AGENTS.md file into the agent state.

        Args:
            state (AgentState): The state of the agent.
            runtime (Runtime[RuntimeCtx]): The runtime context containing the repository id.

        Returns:
            dict[str, Any] | None: The state updates with the agents instructions from the AGENTS.md file.
        """
        agents_md_content = self._get_agents_md_content(
            Path(runtime.context.repo.working_dir), runtime.context.config.context_file_name
        )

        if not agents_md_content:
            return None

        prepend_messages = [
            HumanMessage(
                content=dedent(
                    """
                    # AGENTS.md

                    Here are instructions extracted from the AGENTS.md file for you to follow. If they are contradictory with your own instructions (system prompt), follow your own.

                    ~~~markdown
                    {agents_md_content}
                    ~~~
                    """  # noqa: E501
                ).format(agents_md_content=agents_md_content)
            )
        ]

        return {"messages": Overwrite(prepend_messages + state["messages"])}

    def _get_agents_md_content(self, repo_dir: Path, context_file_name: str | None) -> str | None:
        """
        Get the agent instructions from the AGENTS.md file case insensitive.
        If multiple files are found, return the first one.
        If the file is too long, return the first `max_lines` lines.

        Args:
            context_file_name (str | None): The name of the context file.

        Returns:
            str | None: The agents instructions from the AGENTS.md file.
        """
        if not context_file_name:
            return None

        for path in repo_dir.glob(context_file_name, case_sensitive=False):
            if path.is_file() and path.name.endswith(".md"):
                return "\n".join(path.read_text().splitlines()[:READ_MAX_LINES])
        return None


class AnthropicPromptCachingMiddleware(AnthropicPromptCachingMiddlewareV0):
    """
    Middleware to cache the prompt for the Anthropic model when using OpenRouter.

    This middleware is a wrapper around the LangChain v1 AnthropicPromptCachingMiddleware to support OpenRouter models.

    Example:
        ```python
        from langchain.agents import create_agent

        agent = create_agent(
            model="openai:gpt-4o",
            middleware=[AnthropicPromptCachingMiddleware()],
        )
        ```
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the middleware.
        """
        unsupported_model_behavior = kwargs.pop("unsupported_model_behavior", "ignore")
        super().__init__(*args, unsupported_model_behavior=unsupported_model_behavior, **kwargs)

    def _should_apply_caching(self, request: ModelRequest) -> bool:
        """
        Check if caching should be applied to the request.
        """
        if self._is_openrouter_anthropic_model(request.model):
            messages_count = len(request.messages) + 1 if request.system_prompt else len(request.messages)
            return messages_count >= self.min_messages_to_cache
        return super()._should_apply_caching(request)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """
        Apply cache control to the request.
        """
        if self._is_openrouter_anthropic_model(request.model) and self._should_apply_caching(request):
            for message in reversed(request.messages):
                if message.content_blocks and "cache_control" in message.content_blocks[-1]:
                    del message.content_blocks[-1]["cache_control"]

            if isinstance(request.messages[-1].content, str):
                request.messages[-1].content = [create_text_block(request.messages[-1].content)]

            request.messages[-1].content[-1]["cache_control"] = {"type": self.type, "ttl": self.ttl}
            return await handler(request)
        return await super().awrap_model_call(request, handler)

    def _is_openrouter_anthropic_model(self, model: BaseChatModel) -> bool:
        """
        Check if the model is an OpenRouter Anthropic model.
        """
        return isinstance(model, ChatOpenAI) and model.model_name.startswith(ModelProvider.ANTHROPIC.value)
