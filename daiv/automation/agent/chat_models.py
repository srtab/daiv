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

Display and transport are stored separately, because the display string may be
assembled from stream deltas and is therefore not guaranteed byte-identical to
what the provider emitted — see ``DISPLAY_KEY`` / ``DETAILS_KEY`` /
``FALLBACK_KEY`` below. Only the transport fields are ever sent back.

Round-tripping matters because OpenRouter requires the reasoning that led to a
tool call be echoed back alongside that call's results, unmodified and in order;
otherwise the model re-derives from scratch every turn, at a documented cost of
degraded coherence and lost cache hits. This is Z.ai's "interleaved thinking"
(supported since GLM-4.5, on by default for GLM-4.7/5.x), and the same mechanism
carries Anthropic's signed thinking blocks when routing to ``anthropic/…``.
Upstream ``_convert_message_to_dict`` strips ``additional_kwargs`` wholesale, so
:meth:`ChatOpenRouter._get_request_payload` re-attaches the blocks afterwards.

Every stream override here — reasoning capture, and the metadata dedupe below —
wraps both transports. ``ainvoke`` is not a way around them: upstream's
``_should_stream`` routes it through ``_astream`` whenever a streaming callback
handler is attached, which is every call under LangGraph. The sync ``.invoke()``
in ``automation.titling.tasks`` runs outside that, and reaches ``_stream``.

Round-tripping does **not** change how many reasoning tokens the model spends on
later turns — measured A/B on ``z-ai/glm-5.2``, reasoning-token counts per turn
are the same whether or not prior blocks are echoed back. Don't reach for this as
a fix for a model that stops thinking mid-trajectory.

The chunk merge that accumulates those reasoning deltas — ``merge_dicts``, which
appends same-key strings — is also a hazard, and the reason
:meth:`ChatOpenRouter._stream` exists. It exempts from concatenation only
same-valued ``id`` / ``output_version`` / ``model_provider`` (plus an ``index``
whose value is ``lc_``-prefixed), while ``langchain_openai`` re-stamps
``FINISH_STAMPED_KEYS`` onto *every* chunk carrying a ``finish_reason``.
OpenRouter sends more than one such chunk for some upstream providers, so an
accumulated message reported ``"z-ai/glm-5.2z-ai/glm-5.2"`` and ``"stopstop"``.
A doubled model name resolves to no price and no context window, so a turn's cost
was dropped and the chat's context meter lost its scale.

Repairing it here rather than only where the value is read keeps the corruption
out of checkpoints and traces, and covers all four keys at once. It does not
remove the need for the read-side repair
(``usage_tracking.collapse_repeated_model_name``): an OpenAI-compatible gateway
configured as ``provider_type=OPENAI`` gets a plain ``ChatOpenAI`` and never
passes through this class, and rows written before this fix still hold doubled
names.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from langchain_openai import ChatOpenAI

from core.models import ProviderType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import openai
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.outputs import ChatGenerationChunk, ChatResult

logger = logging.getLogger("daiv.automation")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Stamped by ``langchain_openai._convert_chunk_to_generation_chunk`` inside its
# ``if finish_reason := choice.get("finish_reason")`` block — see the module docstring.
FINISH_STAMPED_KEYS = ("finish_reason", "model_name", "system_fingerprint", "service_tier")

# ``parsed`` may hold arbitrary Pydantic models from structured output; dumping it
# is both wasteful and failure-prone, and upstream excludes it for the same reason.
_RESPONSE_DUMP_EXCLUDE = {"choices": {"__all__": {"message": {"parsed"}}}}

# Display. Rendered by ag_ui_langgraph and chat.turns. May be assembled from stream
# deltas, so it is never authoritative for replay.
DISPLAY_KEY = "reasoning_content"
# Transport, structured. Named for the wire field it serializes straight back to.
DETAILS_KEY = "reasoning_details"
# Transport, plain-string fallback for providers returning no structured blocks.
# Prefixed because a bare ``reasoning`` key already denotes the OpenAI-legacy
# ``{"summary": [...]}`` shape that chat.turns reads.
FALLBACK_KEY = "openrouter_reasoning"


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

    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        seen: dict[str, str] = {}
        for chunk in super()._stream(*args, **kwargs):
            self._drop_restamped_keys(chunk, seen)
            yield chunk

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        seen: dict[str, str] = {}
        async for chunk in super()._astream(*args, **kwargs):
            self._drop_restamped_keys(chunk, seen)
            yield chunk

    @staticmethod
    def _drop_restamped_keys(chunk: ChatGenerationChunk, seen: dict[str, str]) -> None:
        """Drop the finish-stamped keys this stream already emitted, first occurrence winning.

        Mutates both arguments. ``seen`` belongs to the calling generator's frame, never to the
        model instance, which is shared across concurrent requests. Per key rather than
        all-or-nothing, so a key only a later chunk carries is still a first occurrence.

        A repeat carrying a *different* value is a real mid-stream routing switch, not the
        re-stamping this exists for: first-wins still applies (the alternative concatenation
        resolves to no model at all) but it is logged, since otherwise nothing records that a
        second model ran. ``finish_reason`` is exempt — ``tool_calls`` then ``stop`` is
        legitimate, and nothing in DAIV reads it.
        """
        if not (info := chunk.generation_info):
            return
        for key in FINISH_STAMPED_KEYS:
            if key not in info:
                continue
            if key not in seen:
                seen[key] = info[key]
                continue
            if info[key] != seen[key] and key != "finish_reason":
                logger.warning("Stream reported %s %r after %r; keeping the first", key, info[key], seen[key])
            del info[key]

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if generation_chunk is None:
            return None
        self._store_reasoning(self._delta(chunk), generation_chunk.message.additional_kwargs)
        return generation_chunk

    def _create_chat_result(self, response: dict | openai.BaseModel, generation_info: dict | None = None) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        if isinstance(response, dict):
            raw = response
        else:
            raw = response.model_dump(exclude=_RESPONSE_DUMP_EXCLUDE, warnings=False)
        for choice, generation in zip(raw.get("choices") or [], result.generations, strict=True):
            self._store_reasoning((choice or {}).get("message") or {}, generation.message.additional_kwargs)
        return result

    def _get_request_payload(self, input_: LanguageModelInput, *, stop: list[str] | None = None, **kwargs: Any) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        serialized = payload.get("messages")
        if not serialized:
            return payload
        source = self._convert_input(input_).to_messages()
        if len(source) != len(serialized):
            # Responses-API or any future 1:N serialization. Dropping reasoning degrades
            # the turn; misaligning it onto the wrong turn corrupts the trajectory.
            logger.warning(
                "Skipping OpenRouter reasoning replay: serialization changed the message count (%d source, %d wire).",
                len(source),
                len(serialized),
            )
            return payload
        for original, message in zip(source, serialized, strict=True):
            if message.get("role") != "assistant":
                continue
            additional_kwargs = getattr(original, "additional_kwargs", None) or {}
            if details := additional_kwargs.get(DETAILS_KEY):
                message[DETAILS_KEY] = deepcopy(details)
            elif reasoning := additional_kwargs.get(FALLBACK_KEY):
                message["reasoning"] = reasoning
        return payload

    @staticmethod
    def _store_reasoning(source: dict, additional_kwargs: dict) -> None:
        """Split a provider message (or stream delta) into display and transport fields.

        ``source`` is treated as opaque: blocks are deep-copied verbatim, never rewritten,
        reordered, filtered, or reconstructed from the display string.
        """
        if isinstance(details := source.get(DETAILS_KEY), list) and details:
            # Blocks carry an ``index``; merge_dicts merges same-index blocks, so partial
            # blocks reassemble across stream chunks rather than appending as duplicates.
            additional_kwargs[DETAILS_KEY] = deepcopy(details)
        if isinstance(reasoning := source.get("reasoning"), str) and reasoning:
            # String values concatenate when AIMessageChunks are added, so per-chunk
            # deltas accumulate into the final message.
            additional_kwargs[DISPLAY_KEY] = reasoning
            additional_kwargs[FALLBACK_KEY] = reasoning

    @staticmethod
    def _delta(chunk: dict) -> dict:
        """The ``delta`` of a raw stream chunk, or ``{}``.

        Only reached after the parent parsed the same chunk into a non-None generation
        chunk, so ``chunk`` is a well-formed dict; the ``or`` guards cover the
        empty-choices (usage-only) chunk that the parent still passes through.
        """
        return (chunk.get("choices") or [{}])[0].get("delta") or {}
