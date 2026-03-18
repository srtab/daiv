from unittest.mock import AsyncMock, Mock

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware


class _DummyTool(BaseTool):
    name: str = "test_tool"
    description: str = "A test tool"

    def _run(self, *args, **kwargs):
        return ""


class TestAnthropicPromptCachingMiddleware:
    def test_should_apply_caching_counts_system_message_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware(min_messages_to_cache=2)
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
        )

        assert middleware._should_apply_caching(request) is True

    async def test_apply_caching_adds_cache_control_to_extra_body_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
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

    async def test_apply_caching_merges_extra_body_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
            model_settings={"extra_body": {"reasoning": {"effort": "high"}}},
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        wrapped_request = handler.await_args.args[0]

        assert wrapped_request.model_settings == {
            "extra_body": {
                "reasoning": {"effort": "high"},
                "cache_control": {"type": middleware.type, "ttl": middleware.ttl},
            }
        }

    async def test_apply_caching_tags_system_message_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        wrapped_request = handler.await_args.args[0]

        assert wrapped_request.system_message.content == [
            {"type": "text", "text": "system prompt", "cache_control": {"type": middleware.type, "ttl": middleware.ttl}}
        ]

    async def test_apply_caching_tags_tools_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        tool = _DummyTool()

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
            tools=[tool],
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        wrapped_request = handler.await_args.args[0]

        assert len(wrapped_request.tools) == 1
        assert wrapped_request.tools[0].extras == {"cache_control": {"type": middleware.type, "ttl": middleware.ttl}}
        # Original tool should not be mutated
        assert tool.extras is None

    async def test_apply_caching_passes_through_non_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=False)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
            model_settings={"temperature": 0.3},
        )
        handler = AsyncMock(return_value=ModelResponse(result=[]))

        await middleware.awrap_model_call(request, handler)

        assert handler.await_args.args[0] is request

    def test_sync_wrap_model_call_applies_caching_for_openrouter_models(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        request = ModelRequest(
            model=Mock(),
            messages=[HumanMessage(content="user")],
            system_message=SystemMessage(content="system prompt"),
            state=Mock(),
            runtime=Mock(),
            model_settings={"temperature": 0.3},
        )
        handler = Mock(return_value=ModelResponse(result=[]))

        middleware.wrap_model_call(request, handler)

        wrapped_request = handler.call_args.args[0]

        assert wrapped_request is not request
        assert wrapped_request.model_settings == {
            "temperature": 0.3,
            "extra_body": {"cache_control": {"type": middleware.type, "ttl": middleware.ttl}},
        }
