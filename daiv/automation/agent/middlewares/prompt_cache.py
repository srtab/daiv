from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from langchain_anthropic.middleware.prompt_caching import (
    AnthropicPromptCachingMiddleware as AnthropicPromptCachingMiddlewareV0,
)

from automation.agent.chat_models import ChatOpenRouter

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

    def __init__(self, *args, ttl: Literal["5m", "1h"] = "5m", **kwargs):
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

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelCallResult:
        """
        Apply cache control to the request.
        """
        if self._is_openrouter_anthropic_model(request.model) and self._should_apply_caching(request):
            return handler(self._with_extra_body_cache_control(request))
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """
        Apply cache control to the request.
        """
        if self._is_openrouter_anthropic_model(request.model) and self._should_apply_caching(request):
            return await handler(self._with_extra_body_cache_control(request))
        return await super().awrap_model_call(request, handler)

    def _with_extra_body_cache_control(self, request: ModelRequest) -> ModelRequest:
        """
        Pass ``cache_control`` through ``extra_body``: OpenRouter takes it top-level in the request body, which
        ``ChatOpenAI`` only reaches through ``extra_body``.
        """
        existing_extra_body = request.model_settings.get("extra_body", {})
        return request.override(
            model_settings={
                **request.model_settings,
                "extra_body": {**existing_extra_body, "cache_control": {"type": self.type, "ttl": self.ttl}},
            }
        )

    def _is_openrouter_anthropic_model(self, model: BaseChatModel) -> bool:
        """
        Check if the model is an OpenRouter Anthropic model.
        """
        return isinstance(model, ChatOpenRouter) and model.is_anthropic
