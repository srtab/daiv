from unittest.mock import AsyncMock, Mock

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage
from langchain_openrouter import ChatOpenRouter

from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware


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

    async def test_awrap_model_call_adds_cache_control_to_model_settings_for_openrouter_models(self):
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
            "cache_control": {"type": middleware.type, "ttl": middleware.ttl},
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

    def test_is_openrouter_anthropic_model_with_chat_openrouter(self):
        middleware = AnthropicPromptCachingMiddleware()
        model = Mock(spec=ChatOpenRouter)
        model.model_name = "anthropic/claude-sonnet-4.5"

        assert middleware._is_openrouter_anthropic_model(model) is True

    def test_is_openrouter_anthropic_model_with_non_anthropic_model(self):
        middleware = AnthropicPromptCachingMiddleware()
        model = Mock(spec=ChatOpenRouter)
        model.model_name = "openai/gpt-4o"

        assert middleware._is_openrouter_anthropic_model(model) is False

    def test_is_openrouter_anthropic_model_with_non_openrouter_model(self):
        middleware = AnthropicPromptCachingMiddleware()
        model = Mock()  # not a ChatOpenRouter instance

        assert middleware._is_openrouter_anthropic_model(model) is False
