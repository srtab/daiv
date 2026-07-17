"""OpenRouter chat model.

OpenRouter speaks the OpenAI Chat Completions API, so DAIV reaches it through a
:class:`~langchain_openai.ChatOpenAI` *subclass* rather than a dedicated
third-party package. Staying a ``ChatOpenAI`` subclass is load-bearing:
:class:`~automation.agent.middlewares.prompt_cache.AnthropicPromptCachingMiddleware`
detects OpenRouter-Anthropic models with ``isinstance(model, ChatOpenAI)`` and
injects top-level ``extra_body`` ``cache_control`` — the first-party
``langchain-openrouter`` (a ``BaseChatModel`` whose automatic Anthropic caching
is still an open upstream issue) would silently disable that.

This class owns the OpenRouter-specific transport bits that would otherwise be
set ad-hoc at construction time: the base-URL default, and — the reason the
subclass exists — extraction of OpenRouter's non-standard streaming reasoning.
Stock ``ChatOpenAI`` targets the official OpenAI spec only and drops provider
extensions like ``reasoning`` / ``reasoning_details`` by design (see its module
docstring), so the reasoning never reaches ``ag_ui_langgraph`` and the chat UI
shows nothing. Surfacing it as ``additional_kwargs["reasoning_content"]`` (a
string) is the shape both ``ag_ui_langgraph.resolve_reasoning_content`` (live
``REASONING_*`` events) and ``chat.turns`` (transcript reload) already render.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_openai import ChatOpenAI

from core.models import ProviderType

if TYPE_CHECKING:
    from langchain_core.outputs import ChatGenerationChunk

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class ChatOpenRouter(ChatOpenAI):
    """``ChatOpenAI`` pointed at OpenRouter, with reasoning extraction."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("openai_api_base", OPENROUTER_BASE_URL)
        super().__init__(**kwargs)

    @property
    def is_anthropic(self) -> bool:
        """Whether this routes to an Anthropic model (``anthropic/…``). Drives the
        OpenRouter-Anthropic branch of the prompt-caching middleware."""
        return self.model_name.startswith(ProviderType.ANTHROPIC.value)

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is None:
            return None
        reasoning = self._reasoning_delta(chunk)
        if reasoning:
            # String values concatenate when AIMessageChunks are added, so per-chunk
            # deltas accumulate into the final message's reasoning_content.
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    @staticmethod
    def _reasoning_delta(chunk: dict) -> str:
        """The OpenRouter ``reasoning`` text on a raw stream chunk, or ``""``.

        Only reached after the parent parsed the same chunk into a non-None generation
        chunk, so ``chunk`` is a well-formed dict; the ``or`` guards cover the
        empty-choices (usage-only) chunk that the parent still passes through.
        """
        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
        reasoning = delta.get("reasoning")
        return reasoning if isinstance(reasoning, str) else ""
