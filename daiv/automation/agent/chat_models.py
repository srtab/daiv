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
subclass exists — handling of OpenRouter's non-standard reasoning. Stock
``ChatOpenAI`` targets the official OpenAI spec only and drops provider
extensions like ``reasoning`` / ``reasoning_details`` by design (see its module
docstring).

Two fields are captured, for two different consumers:

``reasoning_content`` (string)
    Display only. ``ag_ui_langgraph.resolve_reasoning_content`` (live
    ``REASONING_*`` events) and ``chat.turns`` (transcript reload) both render
    this shape.

``reasoning_details`` (list of blocks)
    Round-trip. OpenRouter requires the reasoning that led to a tool call be
    echoed back alongside that call's results, unmodified and in order —
    otherwise the model re-derives from scratch every turn, at a documented cost
    of degraded coherence and lost cache hits. This is Z.ai's "interleaved
    thinking" (supported since GLM-4.5, on by default for GLM-4.7/5.x), and the
    same mechanism carries Anthropic's signed thinking blocks when routing to
    ``anthropic/…``. Upstream ``_convert_message_to_dict`` strips
    ``additional_kwargs`` wholesale, so :meth:`ChatOpenRouter._get_request_payload`
    re-attaches the blocks after serialization.

Capture covers both transports: subagents call ``ainvoke``, so a streaming-only
hook would leave every subagent turn reasoning-blind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_openai import ChatOpenAI

from core.models import ProviderType

if TYPE_CHECKING:
    import openai
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.outputs import ChatGenerationChunk, ChatResult

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ``parsed`` may hold arbitrary Pydantic models from structured output; dumping it
# is both wasteful and failure-prone, and upstream excludes it for the same reason.
_RESPONSE_DUMP_EXCLUDE = {"choices": {"__all__": {"message": {"parsed"}}}}


class ChatOpenRouter(ChatOpenAI):
    """``ChatOpenAI`` pointed at OpenRouter, with reasoning capture and round-trip."""

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
        delta = self._delta(chunk)
        if (reasoning := delta.get("reasoning")) and isinstance(reasoning, str):
            # String values concatenate when AIMessageChunks are added, so per-chunk
            # deltas accumulate into the final message's reasoning_content.
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        if details := delta.get("reasoning_details"):
            # Blocks carry an ``index``; merge_dicts merges same-index blocks, so partial
            # blocks reassemble across chunks rather than appending as duplicates.
            generation_chunk.message.additional_kwargs["reasoning_details"] = details
        return generation_chunk

    def _create_chat_result(self, response: dict | openai.BaseModel, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        if isinstance(response, dict):
            raw = response
        else:
            raw = response.model_dump(exclude=_RESPONSE_DUMP_EXCLUDE, warnings=False)
        message = ((raw.get("choices") or [{}])[0] or {}).get("message") or {}
        if not (result.generations and message):
            return result
        additional_kwargs = result.generations[0].message.additional_kwargs
        if details := message.get("reasoning_details"):
            additional_kwargs["reasoning_details"] = details
        if (reasoning := message.get("reasoning")) and isinstance(reasoning, str):
            additional_kwargs.setdefault("reasoning_content", reasoning)
        return result

    def _get_request_payload(self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        serialized = payload.get("messages")
        if not serialized:
            return payload
        source = self._convert_input(input_).to_messages()
        if len(source) != len(serialized):
            # Responses-API or any future 1:N serialization — bail rather than
            # misalign reasoning onto the wrong turn.
            return payload
        for original, message in zip(source, serialized, strict=True):
            if message.get("role") != "assistant":
                continue
            if details := (getattr(original, "additional_kwargs", None) or {}).get("reasoning_details"):
                message["reasoning_details"] = details
        return payload

    @staticmethod
    def _delta(chunk: dict) -> dict:
        """The ``delta`` of a raw stream chunk, or ``{}``.

        Only reached after the parent parsed the same chunk into a non-None generation
        chunk, so ``chunk`` is a well-formed dict; the ``or`` guards cover the
        empty-choices (usage-only) chunk that the parent still passes through.
        """
        return (chunk.get("choices") or [{}])[0].get("delta") or {}
