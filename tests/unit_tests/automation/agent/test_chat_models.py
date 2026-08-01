"""Tests for the OpenRouter chat model subclass.

Covers only DAIV's custom behavior (per project convention): reasoning capture
(streaming and not) and round-trip, the ``is_anthropic`` family flag, and the
OpenRouter base-URL default. The upstream ChatOpenAI machinery is not re-tested.
"""

from langchain_core.load import dumpd, load
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI

from automation.agent.chat_models import DETAILS_KEY, FALLBACK_KEY, OPENROUTER_BASE_URL, ChatOpenRouter

# Verbatim shape returned by openrouter for z-ai/glm-5.2 (no signature/id on this
# provider; anthropic/… blocks add them, and are echoed back the same way).
REASONING_BLOCK = {"type": "reasoning.text", "text": "Lisbon — call get_weather.", "format": "unknown", "index": 0}

# Carries fields DAIV knows nothing about, so any sanitizing or reconstructing shows
# up as a failure instead of passing silently.
OPAQUE_BLOCKS = [
    {
        "type": "reasoning.text",
        "text": "Inspect the base interface.",
        "provider": "z-ai",
        "opaque_metadata": {"sequence": 7, "signature": "do-not-modify"},
    },
    {"type": "reasoning.text", "text": "Then compare one sibling.", "provider_extension": ["a", "b"]},
]


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

    def test_captures_reasoning_string_for_display_and_transport(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        result = model._create_chat_result(self._response({"content": "hi", "reasoning": "thinking"}))

        additional_kwargs = result.generations[0].message.additional_kwargs
        assert additional_kwargs["reasoning_content"] == "thinking"
        assert additional_kwargs[FALLBACK_KEY] == "thinking"

    def test_response_without_reasoning_is_untouched(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        result = model._create_chat_result(self._response({"content": "hi"}))

        assert result.generations[0].message.additional_kwargs == {}


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


class TestChatOpenRouterStreamingDetails:
    def test_preserves_streaming_reasoning_details(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        gen = model._convert_chunk_to_generation_chunk(_chunk({"reasoning_details": OPAQUE_BLOCKS}), AIMessageChunk, {})

        assert gen.message.additional_kwargs[DETAILS_KEY] == OPAQUE_BLOCKS

    def test_reasoning_details_merge_in_original_order(self):
        """Blocks carry an ``index``; same-index deltas reassemble, distinct ones append
        in provider order rather than duplicating."""
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")

        g1 = model._convert_chunk_to_generation_chunk(
            _chunk({"reasoning_details": [{"type": "reasoning.text", "text": "first ", "index": 0}]}),
            AIMessageChunk,
            {},
        )
        g2 = model._convert_chunk_to_generation_chunk(
            _chunk({"reasoning_details": [{"type": "reasoning.text", "text": "block", "index": 0}]}), AIMessageChunk, {}
        )
        g3 = model._convert_chunk_to_generation_chunk(
            _chunk({"reasoning_details": [{"type": "reasoning.text", "text": "second", "index": 1}]}),
            AIMessageChunk,
            {},
        )

        merged = g1.message + g2.message + g3.message

        assert merged.additional_kwargs[DETAILS_KEY] == [
            {"type": "reasoning.text", "text": "first block", "index": 0},
            {"type": "reasoning.text", "text": "second", "index": 1},
        ]

    def test_stored_details_are_isolated_from_the_provider_payload(self):
        """Blocks are deep-copied on capture, so later mutation of the raw chunk (or of
        an outbound payload built from it) cannot reach into agent state."""
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        blocks = [{"type": "reasoning.text", "text": "original", "index": 0}]

        gen = model._convert_chunk_to_generation_chunk(_chunk({"reasoning_details": blocks}), AIMessageChunk, {})
        blocks[0]["text"] = "mutated"

        assert gen.message.additional_kwargs[DETAILS_KEY][0]["text"] == "original"


class TestChatOpenRouterReasoningFallback:
    def test_replays_reasoning_string_when_details_are_absent(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        message = AIMessage(content="hi", additional_kwargs={FALLBACK_KEY: "plain reasoning"})

        payload = model._get_request_payload([HumanMessage("hi"), message])

        assert payload["messages"][1]["reasoning"] == "plain reasoning"
        assert DETAILS_KEY not in payload["messages"][1]

    def test_prefers_reasoning_details_over_reasoning_string(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        message = AIMessage(
            content="hi", additional_kwargs={FALLBACK_KEY: "plain reasoning", DETAILS_KEY: OPAQUE_BLOCKS}
        )

        payload = model._get_request_payload([HumanMessage("hi"), message])

        assert payload["messages"][1][DETAILS_KEY] == OPAQUE_BLOCKS
        assert "reasoning" not in payload["messages"][1]


class TestChatOpenRouterOpaqueness:
    def test_unknown_provider_fields_survive_capture_and_replay(self):
        """The raw provider blocks, the stored value and the outbound value must be
        deep-equal — nothing sanitized, reordered or reconstructed from the display text."""
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        response = {
            "id": "c",
            "model": "z-ai/glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning": "Inspect the base interface.",
                        "reasoning_details": OPAQUE_BLOCKS,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"path": "base.py"}'},
                            }
                        ],
                    },
                }
            ],
        }

        captured = model._create_chat_result(response).generations[0].message
        payload = model._get_request_payload([
            HumanMessage("review it"),
            captured,
            ToolMessage(content="class Handler: ...", tool_call_id="call_123"),
        ])

        assert captured.additional_kwargs[DETAILS_KEY] == OPAQUE_BLOCKS
        assert payload["messages"][1][DETAILS_KEY] == OPAQUE_BLOCKS

    def test_outbound_payload_does_not_alias_message_state(self):
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        message = AIMessage(content="hi", additional_kwargs={DETAILS_KEY: [dict(REASONING_BLOCK)]})

        payload = model._get_request_payload([HumanMessage("hi"), message])
        payload["messages"][1][DETAILS_KEY][0]["text"] = "tampered"

        assert message.additional_kwargs[DETAILS_KEY][0]["text"] == REASONING_BLOCK["text"]


class TestChatOpenRouterSerialization:
    def test_reasoning_transport_fields_survive_message_serialization(self):
        """Agent state is checkpointed through LangChain's serializer; transport fields
        are plain JSON, but assert it rather than assume it."""
        message = AIMessage(
            content="",
            additional_kwargs={
                "reasoning_content": "display text",
                FALLBACK_KEY: "display text",
                DETAILS_KEY: OPAQUE_BLOCKS,
            },
            tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "t1", "type": "tool_call"}],
        )

        restored = load(dumpd(message))

        assert restored.additional_kwargs[DETAILS_KEY] == OPAQUE_BLOCKS
        assert restored.additional_kwargs[FALLBACK_KEY] == "display text"
        assert restored.tool_calls[0]["id"] == "t1"


class TestChatOpenRouterToolCycle:
    def test_reasoning_is_replayed_after_tool_result(self):
        """The regression test: provider returns reasoning + a tool call, a ToolMessage is
        appended, and the next request carries that reasoning on the assistant turn that
        made the call — not on the tool message."""
        model = ChatOpenRouter(model="z-ai/glm-5.2", api_key="x")
        provider_response = {
            "id": "c",
            "model": "z-ai/glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning": "I need to inspect the interface.",
                        "reasoning_details": [{"type": "reasoning.text", "text": "I need to inspect the interface."}],
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": '{"file_path": "/repo/file.py"}'},
                            }
                        ],
                    },
                }
            ],
        }

        assistant = model._create_chat_result(provider_response).generations[0].message
        payload = model._get_request_payload([
            HumanMessage("review /repo/file.py"),
            assistant,
            ToolMessage(content="print('hi')", tool_call_id="call_123"),
        ])

        wire_assistant, wire_tool = payload["messages"][1], payload["messages"][2]
        assert wire_assistant["role"] == "assistant"
        assert wire_assistant[DETAILS_KEY] == [{"type": "reasoning.text", "text": "I need to inspect the interface."}]
        assert wire_assistant["tool_calls"][0]["id"] == "call_123"
        assert wire_tool["role"] == "tool"
        assert DETAILS_KEY not in wire_tool
        assert "reasoning" not in wire_tool


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
