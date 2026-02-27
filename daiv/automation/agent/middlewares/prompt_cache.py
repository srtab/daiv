from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

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

    def __init__(self, *args, ttl: Literal["5m", "1h"] = "1h", **kwargs):
        """
        Initialize the middleware.
        """
        unsupported_model_behavior = kwargs.pop("unsupported_model_behavior", "ignore")
        super().__init__(*args, unsupported_model_behavior=unsupported_model_behavior, ttl=ttl, **kwargs)

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
                content = message.content
                if isinstance(content, list) and content and isinstance(content[-1], dict):
                    content[-1].pop("cache_control", None)

            cache_target_message = next(
                (message for message in reversed(request.messages) if message.type == "human"), None
            )
            if cache_target_message is None:
                return await handler(request)

            cache_target_content = cache_target_message.content
            normalized_content: list[str | dict[str, Any]]
            if isinstance(cache_target_content, str):
                normalized_content = [cast("dict[str, Any]", create_text_block(cache_target_content))]
            elif not cache_target_content:
                normalized_content = [cast("dict[str, Any]", create_text_block(""))]
            else:
                normalized_content = cast("list[str | dict[str, Any]]", list(cache_target_content))
                if isinstance(normalized_content[-1], str):
                    normalized_content[-1] = cast("dict[str, Any]", create_text_block(normalized_content[-1]))

            cache_target_block = normalized_content[-1]
            if not isinstance(cache_target_block, dict):
                cache_target_block = cast("dict[str, Any]", create_text_block(""))
                normalized_content.append(cache_target_block)
            cache_target_block["cache_control"] = {"type": self.type, "ttl": self.ttl}
            cache_target_message.content = cast("list[str | dict]", normalized_content)
            return await handler(request)
        return await super().awrap_model_call(request, handler)

    def _is_openrouter_anthropic_model(self, model: BaseChatModel) -> bool:
        """
        Check if the model is an OpenRouter Anthropic model.
        """
        return isinstance(model, ChatOpenAI) and model.model_name.startswith(ModelProvider.ANTHROPIC.value)
