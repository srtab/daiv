"""Tests for the OpenRouter chat model subclass.

Covers only DAIV's custom behavior (per project convention): reasoning capture
(streaming and not) and round-trip, the ``is_anthropic`` family flag, and the
OpenRouter base-URL default. The upstream ChatOpenAI machinery is not re-tested.
"""

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from automation.agent.chat_models import OPENROUTER_BASE_URL, ChatOpenRouter

# Verbatim shape returned by openrouter for z-ai/glm-5.2 (no signature/id on this
# provider; anthropic/… blocks add them, and are echoed back the same way).
REASONING_BLOCK = {"type": "reasoning.text", "text": "Lisbon — call get_weather.", "format": "unknown", "index": 0}


def _chunk(delta: dict) -> dict:
    """A raw Chat-Completions stream chunk dict, as the OpenAI SDK hands it to
    ``_convert_chunk_to_generation_chunk`` (model_dump of a ChatCompletionChunk)."""
    return {
        "id": "c",
        "model": "anthropic/claude-haiku-4.5",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


class TestChatOpenRouterReasoning:
    def test_extracts_streaming_reasoning_into_reasoning_content(self):
        model = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")

        gen = model._convert_chunk_to_generation_chunk(
            _chunk({"content": "", "reasoning": "Let me think about 17*23."}), AIMessageChunk, {}
        )

        assert gen is not None
        assert gen.message.additional_kwargs["reasoning_content"] == "Let me think about 17*23."

    def test_content_only_chunk_has_no_reasoning_content(self):
        model = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")

        gen = model._convert_chunk_to_generation_chunk(_chunk({"content": "391"}), AIMessageChunk, {})

        assert gen is not None
        assert "reasoning_content" not in gen.message.additional_kwargs

    def test_reasoning_content_merges_across_chunks(self):
        """AIMessageChunk addition concatenates string additional_kwargs, so the
        per-chunk reasoning deltas accumulate into the final message."""
        model = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")

        g1 = model._convert_chunk_to_generation_chunk(_chunk({"reasoning": "Break it: "}), AIMessageChunk, {})
        g2 = model._convert_chunk_to_generation_chunk(_chunk({"reasoning": "17*(20+3)."}), AIMessageChunk, {})

        merged = g1.message + g2.message
        assert merged.additional_kwargs["reasoning_content"] == "Break it: 17*(20+3)."


class TestChatOpenRouterNonStreamingCapture:
    """Subagents call ``ainvoke``, so capture must not be streaming-only."""

    def _response(self, message: dict) -> dict:
        choice = {"index": 0, "message": {"role": "assistant", **message}}
        return {"id": "c", "model": "z-ai/glm-5.2", "choices": [choice]}

    def test_captures_reasoning_details_from_non_streaming_response(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        result = model._create_chat_result(
            self._response({"content": None, "reasoning_details": [REASONING_BLOCK], "reasoning": "Lisbon."})
        )

        assert result.generations[0].message.additional_kwargs["reasoning_details"] == [REASONING_BLOCK]

    def test_captures_reasoning_string_as_reasoning_content(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        result = model._create_chat_result(self._response({"content": "hi", "reasoning": "thinking"}))

        assert result.generations[0].message.additional_kwargs["reasoning_content"] == "thinking"

    def test_response_without_reasoning_is_untouched(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        result = model._create_chat_result(self._response({"content": "hi"}))

        assert "reasoning_details" not in result.generations[0].message.additional_kwargs
        assert "reasoning_content" not in result.generations[0].message.additional_kwargs


class TestChatOpenRouterReasoningRoundTrip:
    """Upstream ``_convert_message_to_dict`` drops ``additional_kwargs``; the payload
    hook is what puts reasoning back on the wire."""

    def test_reattaches_reasoning_details_to_assistant_message(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        messages = [
            HumanMessage("weather in Lisbon?"),
            AIMessage(
                content="",
                additional_kwargs={"reasoning_details": [REASONING_BLOCK]},
                tool_calls=[{"name": "get_weather", "args": {"city": "Lisbon"}, "id": "t1", "type": "tool_call"}],
            ),
            ToolMessage(content='{"temp_c": 19}', tool_call_id="t1"),
        ]

        payload = model._get_request_payload(messages)

        assistant = payload["messages"][1]
        assert assistant["reasoning_details"] == [REASONING_BLOCK]
        assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_leaves_non_assistant_messages_alone(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        payload = model._get_request_payload([HumanMessage("hi"), ToolMessage(content="{}", tool_call_id="t1")])

        assert all("reasoning_details" not in message for message in payload["messages"])

    def test_assistant_without_reasoning_gets_no_field(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        payload = model._get_request_payload([HumanMessage("hi"), AIMessage(content="hello")])

        assert "reasoning_details" not in payload["messages"][1]

    def test_display_only_reasoning_content_is_not_sent(self):
        """``reasoning_content`` feeds the chat UI; only ``reasoning_details`` round-trips."""
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        payload = model._get_request_payload([
            HumanMessage("hi"),
            AIMessage(content="hello", additional_kwargs={"reasoning_content": "thinking"}),
        ])

        assert "reasoning_content" not in payload["messages"][1]


class TestChatOpenRouterFamily:
    def test_is_anthropic_true_for_anthropic_model(self):
        assert ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x").is_anthropic is True

    def test_is_anthropic_false_for_non_anthropic_model(self):
        assert ChatOpenRouter(model="openai/gpt-5.4", api_key="x").is_anthropic is False


class TestChatOpenRouterDefaults:
    def test_defaults_base_url_to_openrouter(self):
        model = ChatOpenRouter(model="anthropic/claude-haiku-4.5", api_key="x")
        assert model.openai_api_base == OPENROUTER_BASE_URL

    def test_explicit_base_url_is_respected(self):
        model = ChatOpenRouter(
            model="anthropic/claude-haiku-4.5", api_key="x", openai_api_base="https://proxy.example/v1"
        )
        assert model.openai_api_base == "https://proxy.example/v1"

    def test_is_a_chat_openai_subclass(self):
        # Load-bearing: AnthropicPromptCachingMiddleware detects OpenRouter-Anthropic
        # models via isinstance(model, ChatOpenAI).
        assert issubclass(ChatOpenRouter, ChatOpenAI)
