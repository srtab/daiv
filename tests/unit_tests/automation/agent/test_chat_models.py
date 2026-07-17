"""Tests for the OpenRouter chat model subclass.

Covers only DAIV's custom behavior (per project convention): reasoning
extraction, the ``is_anthropic`` family flag, and the OpenRouter base-URL
default. The upstream ChatOpenAI streaming machinery is not re-tested.
"""

from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI

from automation.agent.chat_models import OPENROUTER_BASE_URL, ChatOpenRouter


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
