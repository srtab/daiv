from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from langchain_anthropic.middleware.prompt_caching import (
    AnthropicPromptCachingMiddleware as AnthropicPromptCachingMiddlewareV0,
)
from langchain_anthropic.middleware.prompt_caching import _tag_system_message, _tag_tools
from langchain_openai.chat_models import ChatOpenAI

from automation.agent.base import ModelProvider

if TYPE_CHECKING:
    from langchain.agents.middleware import ModelRequest
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
            messages_count = len(request.messages) + 1 if request.system_message else len(request.messages)
            return messages_count >= self.min_messages_to_cache
        return super()._should_apply_caching(request)

    def _apply_caching(self, request: ModelRequest) -> ModelRequest:
        """
        Apply cache control to the request for OpenRouter models.

        For OpenRouter models, merges cache_control into extra_body within model_settings,
        and tags the system message and tools with cache_control.
        For non-OpenRouter models, delegates to the parent implementation.
        """
        if not self._is_openrouter_anthropic_model(request.model):
            return super()._apply_caching(request)

        overrides: dict[str, Any] = {}
        cache_control = self._cache_control

        existing_extra_body = request.model_settings.get("extra_body", {})
        overrides["model_settings"] = {
            **request.model_settings,
            "extra_body": {**existing_extra_body, "cache_control": cache_control},
        }

        system_message = _tag_system_message(request.system_message, cache_control)
        if system_message is not request.system_message:
            overrides["system_message"] = system_message

        tools = _tag_tools(request.tools, cache_control)
        if tools is not request.tools:
            overrides["tools"] = tools

        return request.override(**overrides)

    def _is_openrouter_anthropic_model(self, model: BaseChatModel) -> bool:
        """
        Check if the model is an OpenRouter Anthropic model.
        """
        return isinstance(model, ChatOpenAI) and model.model_name.startswith(ModelProvider.ANTHROPIC.value)
