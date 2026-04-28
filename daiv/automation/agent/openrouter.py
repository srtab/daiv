from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_openai.chat_models import ChatOpenAI

if TYPE_CHECKING:
    from langchain_core.outputs import ChatGenerationChunk


class ChatOpenRouter(ChatOpenAI):
    """``ChatOpenAI`` subclass that preserves OpenRouter-specific usage fields.

    OpenRouter returns a ``cost`` field (USD) inside ``usage`` when the request
    sets ``usage: {include: true}``. ``langchain_openai`` strips this when
    converting raw usage to ``UsageMetadata`` while streaming, so the cost
    is lost before our callbacks see it. This subclass stashes the cost on
    the chunk's ``response_metadata`` so it survives chunk merging and is
    reachable from the standard ``on_llm_end`` callback.

    For non-streaming responses, the cost is already present at
    ``message.response_metadata["token_usage"]["cost"]`` via the default
    ``llm_output`` propagation, so no extra handling is needed there.
    """

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        gen_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if gen_chunk is None:
            return None
        cost = (chunk.get("usage") or {}).get("cost")
        if cost is not None:
            gen_chunk.message.response_metadata["openrouter_cost_usd"] = str(cost)
        return gen_chunk
