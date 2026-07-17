from unittest.mock import AsyncMock, Mock

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from automation.agent.chat_models import ChatOpenRouter
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware


class TestIsOpenRouterAnthropicModel:
    def test_detects_chat_openrouter_anthropic_model(self):
        middleware = AnthropicPromptCachingMiddleware()
        model = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")
        assert middleware._is_openrouter_anthropic_model(model) is True

    def test_ignores_chat_openrouter_non_anthropic_model(self):
        middleware = AnthropicPromptCachingMiddleware()
        model = ChatOpenRouter(model="openai/gpt-5.4", api_key="x")
        assert middleware._is_openrouter_anthropic_model(model) is False

    def test_ignores_plain_chat_openai_even_with_anthropic_name(self):
        # A vanilla ChatOpenAI can't serve Anthropic caching; only our OpenRouter
        # subclass (which forwards extra_body cache_control) should qualify.
        middleware = AnthropicPromptCachingMiddleware()
        model = ChatOpenAI(model="anthropic/claude-haiku-4.5", api_key="x")
        assert middleware._is_openrouter_anthropic_model(model) is False


class TestAnthropicPromptCachingMiddleware:
    def test_should_apply_caching_counts_system_prompt_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware(min_messages_to_cache=2)
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_prompt="system prompt",
            state=Mock(),
            runtime=Mock(),
        )

        assert middleware._should_apply_caching(request) is True

    async def test_awrap_model_call_adds_cache_control_to_extra_body_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_prompt="system prompt",
            state=Mock(),
            runtime=Mock(),
            model_settings={"temperature": 0.3},
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        wrapped_request = handler.await_args.args[0]

        assert wrapped_request is not request
        assert wrapped_request.model_settings == {
            "temperature": 0.3,
            "extra_body": {"cache_control": {"type": middleware.type, "ttl": middleware.ttl}},
        }
        assert request.model_settings == {"temperature": 0.3}

    async def test_awrap_model_call_passes_through_non_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=False)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_prompt="system prompt",
            state=Mock(),
            runtime=Mock(),
            model_settings={"temperature": 0.3},
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        assert handler.await_args.args[0] is request
