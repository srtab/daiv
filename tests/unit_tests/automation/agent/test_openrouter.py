from __future__ import annotations

from langchain_core.messages import AIMessageChunk

from automation.agent.openrouter import ChatOpenRouter


def _stream_chunk(*, content: str = "", usage: dict | None = None, finish_reason: str | None = None) -> dict:
    """Build a raw OpenRouter SSE chunk dict matching the OpenAI streaming schema."""
    chunk: dict = {
        "id": "gen-1",
        "model": "anthropic/claude-sonnet-4.6",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


class TestChatOpenRouterChunkConversion:
    def _client(self) -> ChatOpenRouter:
        # api_key required by the constructor; client is never actually used.
        return ChatOpenRouter(
            model="anthropic/claude-sonnet-4.6", api_key="sk-test", base_url="https://openrouter.test"
        )

    def test_stashes_cost_from_final_chunk(self):
        client = self._client()
        chunk = _stream_chunk(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost": 0.0987},
            finish_reason="stop",
        )

        gen_chunk = client._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, base_generation_info=None)

        assert gen_chunk is not None
        assert gen_chunk.message.response_metadata["openrouter_cost_usd"] == "0.0987"

    def test_no_cost_when_usage_missing(self):
        """Mid-stream chunks have no usage block — leave response_metadata clean."""
        client = self._client()
        chunk = _stream_chunk(content="hello")

        gen_chunk = client._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, base_generation_info=None)

        assert gen_chunk is not None
        assert "openrouter_cost_usd" not in gen_chunk.message.response_metadata

    def test_no_cost_when_usage_lacks_cost_field(self):
        """Older OpenRouter responses without `usage:include` won't have `cost`."""
        client = self._client()
        chunk = _stream_chunk(
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}, finish_reason="stop"
        )

        gen_chunk = client._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, base_generation_info=None)

        assert gen_chunk is not None
        assert "openrouter_cost_usd" not in gen_chunk.message.response_metadata
