from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_anthropic.middleware.prompt_caching import (
    AnthropicPromptCachingMiddleware as AnthropicPromptCachingMiddlewareV0,
)
from langchain_core.messages.content import create_text_block
from langchain_openai.chat_models import ChatOpenAI

from automation.agent.base import ModelProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.language_models import BaseChatModel


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
