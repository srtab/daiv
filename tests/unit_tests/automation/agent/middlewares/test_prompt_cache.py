from unittest.mock import Mock

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware


class TestAnthropicPromptCachingMiddleware:
    async def test_awrap_model_call_uses_latest_non_tool_message_for_cache_control(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        human_message = HumanMessage(
            content=[{"type": "text", "text": "user", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        )
        ai_message = AIMessage(
            content=[{"type": "text", "text": "assistant", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
        )
        tool_message = ToolMessage(
            content=[{"type": "text", "text": "tool output", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            tool_call_id="call_1",
        )
        request = ModelRequest(
            model=Mock(),
            messages=[human_message, ai_message, tool_message],
            system_prompt="system prompt",
            state=Mock(),
            runtime=Mock(),
        )

        async def handler(_request: ModelRequest) -> ModelResponse:
            return ModelResponse(result=[])

        await middleware.awrap_model_call(request, handler)

        assert "cache_control" not in human_message.content[-1]
        assert ai_message.content[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in tool_message.content[-1]

    async def test_awrap_model_call_converts_string_content_for_cache_target(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        human_message = HumanMessage(content="user")
        tool_message = ToolMessage(content=[{"type": "text", "text": "tool output"}], tool_call_id="call_1")
        request = ModelRequest(
            model=Mock(),
            messages=[human_message, tool_message],
            system_prompt="system prompt",
            state=Mock(),
            runtime=Mock(),
        )

        async def handler(_request: ModelRequest) -> ModelResponse:
            return ModelResponse(result=[])

        await middleware.awrap_model_call(request, handler)

        assert isinstance(human_message.content, list)
        assert human_message.content[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        assert "cache_control" not in tool_message.content[-1]

    async def test_awrap_model_call_handles_only_tool_messages(self):
        middleware = AnthropicPromptCachingMiddleware()
        middleware._is_openrouter_anthropic_model = Mock(return_value=True)

        tool_message = ToolMessage(content=[{"type": "text", "text": "tool output"}], tool_call_id="call_1")
        request = ModelRequest(
            model=Mock(), messages=[tool_message], system_prompt="system prompt", state=Mock(), runtime=Mock()
        )

        async def handler(_request: ModelRequest) -> ModelResponse:
            return ModelResponse(result=[])

        await middleware.awrap_model_call(request, handler)

        assert "cache_control" not in tool_message.content[-1]
