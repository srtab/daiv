"""Live probe: does each deferred-tool delivery mode keep the provider's prompt cache across a tool load?

Excluded from ``make test``. Each case replays three scripted model calls, each a strict prefix of the next:

  * A: a short system prompt, the user's request and an ~8k-token file read. Writes the cache.
  * B: A plus the ``tool_search`` call and its result, with the loaded tool delivered the case's way.
  * C: B plus a call to the loaded tool and its result.

A hit means B reads A's prompt from the cache and C reads B's. The ``tool_search`` result embeds the schema in
every case, as production does. The bulk sits in the history, as in a real session: many templates render tools
after the system prompt but ahead of the messages, so appending one there loses the history, not the system
prompt. Cases:

  * inline: the tool is declared mid-conversation right after the ``tool_search`` result (Anthropic
    ``tool_addition``, OpenAI Responses ``additional_tools``); on Anthropic the ``inline-tools`` beta header rides
    on every call.
  * inline header flip (the first Anthropic inline model only): the beta header first appears on B, as
    langchain-anthropic adds it only once a tool block is in the request. Anthropic doesn't document whether that
    resets the cache; it must not.
  * frozen: the tools array never changes; the schema reaches the model only through the ``tool_search`` result.
    Held to the same bar, so only for providers whose cache matches exact prefixes (Claude by default).
  * freezing vs appending, for every frozen-list model and candidate: after A, B and C go out once with the tool
    appended to the tools array and once frozen, and the frozen pair must read more from the cache in total.
    Counting C matters: its history calls a tool the frozen array never declared, and a provider that misses there
    gives back what freezing saved on B. Best-effort or coarse caches (DeepSeek, Gemini) miss the 80% bar now and
    then, yet freezing still pays off wherever it beats appending.

Every call logs its generation id; on OpenRouter, look it up to see which upstream provider served it. A model the
account can't use (403) is skipped, not failed.

Run with live logs to see every call's numbers::

    uv run pytest --envfile +docker/local/app/config.secrets.env --no-cov --log-cli-level=INFO \\
        tests/integration_tests/test_deferred_tools_cache.py -m deferred_cache

Models come from ``DAIV_EVAL_DEFERRED_CACHE_INLINE_MODELS``, ``DAIV_EVAL_DEFERRED_CACHE_FROZEN_MODELS`` and the
frozen gate's lists in ``utils.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, NamedTuple

import pytest
from langchain_anthropic import ChatAnthropic, convert_to_anthropic_tool
from langchain_anthropic.chat_models import _INLINE_TOOLS_BETA, _supports_mid_conversation_system_messages
from langchain_core.exceptions import ModelPermissionDeniedError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI

from automation.agent import BaseAgent
from automation.agent.chat_models import ChatOpenRouter
from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block
from automation.agent.deferred.search_tool import TOOL_SEARCH_NAME, make_tool_search

from .deferred_tools import TOOL_NAME, digest_tool, tool_search_result
from .utils import (
    DEFERRED_CACHE_FROZEN_MODELS,
    DEFERRED_CACHE_INLINE_MODELS,
    DEFERRED_FROZEN_ALLOWLISTED,
    DEFERRED_FROZEN_CANDIDATES,
    require_provider_for_model,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

_HIT_RATIO = 0.8
_CACHE_CONTROL = {"type": "ephemeral"}
_FILLER = "\n".join(
    f"Reference note {i}: escalated tickets go through triage, then to the owning queue, before any reply is sent."
    for i in range(300)
)


class _Usage(NamedTuple):
    input: int
    cache_read: int
    generation: str


def _tool_search() -> BaseTool:
    return make_tool_search(lambda: DeferredToolsIndex([digest_tool()]), top_k_default=5, top_k_max=10)


def _read_file(file_path: str) -> str:
    """Read a file from the repository."""
    return ""


def _core_tools() -> list[BaseTool]:
    """The always-loaded tools every call binds, as production binds its file tools and ``tool_search``."""
    return [StructuredTool.from_function(_read_file, name="read_file"), _tool_search()]


def _anthropic_block(tool: BaseTool) -> dict:
    return {
        "type": "tool_addition",
        "tool": {"type": "tool_definition", "definition": dict(convert_to_anthropic_tool(tool))},
    }


def _openai_block(tool: BaseTool) -> dict:
    return {
        "type": "additional_tools",
        "role": "developer",
        "tools": [{"type": "function", **convert_to_openai_tool(tool)["function"]}],
    }


def _inline_model(model_spec: str) -> tuple[BaseChatModel, Callable[[BaseTool], dict]]:
    """The model for ``model_spec`` set up for inline definitions, and the block builder for its provider."""
    model = BaseAgent.get_model(model=model_spec)
    if type(model) is ChatAnthropic:
        if not _supports_mid_conversation_system_messages(model.model):
            pytest.skip(f"langchain-anthropic hoists mid-conversation system messages for {model.model!r}")
        return model, _anthropic_block
    if type(model) is ChatOpenAI and model.use_responses_api:
        return model, _openai_block
    pytest.skip(f"{model_spec} has no native mid-conversation tool definitions")


def _cache_kwargs(model: BaseChatModel, nonce: str) -> dict:
    """Call kwargs that turn on caching the way production's prompt-caching middleware does."""
    if isinstance(model, ChatAnthropic):
        return {"cache_control": _CACHE_CONTROL}
    if isinstance(model, ChatOpenRouter):
        return {"extra_body": {"cache_control": _CACHE_CONTROL}} if model.is_anthropic else {}
    if type(model) is ChatOpenAI and model.use_responses_api:
        return {"prompt_cache_key": nonce}
    return {}


def _prompts(nonce: str, delivery: dict | None = None) -> tuple[list[BaseMessage], ...]:
    """Prompts A, B and C; ``delivery`` is the inline tool block inserted right after the ``tool_search`` result."""
    tool = digest_tool()
    system = SystemMessage(content=f"Session {nonce}.\n\n{build_deferred_tools_block(DeferredToolsIndex([tool]))}")
    request = HumanMessage(
        content="Read the runbook, then fetch a digest of ticket ABC-123, at most 3 notes, and skip resolved children."
    )
    read_call = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "id": "call_rf", "args": {"file_path": "docs/runbook.md"}, "type": "tool_call"}
        ],
    )
    runbook = ToolMessage(content=_FILLER, tool_call_id="call_rf")
    search_call = AIMessage(
        content="",
        tool_calls=[{"name": TOOL_SEARCH_NAME, "id": "call_ts", "args": {"select": [TOOL_NAME]}, "type": "tool_call"}],
    )
    search_result = ToolMessage(content=tool_search_result(tool, embed_schema=True), tool_call_id="call_ts")
    digest_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": TOOL_NAME,
                "id": "call_digest",
                "args": {"ticket": "ABC-123", "note_window": 3, "sweep_closed": False},
                "type": "tool_call",
            }
        ],
    )
    digest_result = ToolMessage(content="ABC-123: 3 notes, no resolved children.", tool_call_id="call_digest")

    prompt_a = [system, request, read_call, runbook]
    prompt_b = [*prompt_a, search_call, search_result, *([SystemMessage(content=[delivery])] if delivery else [])]
    prompt_c = [*prompt_b, digest_call, digest_result]
    return prompt_a, prompt_b, prompt_c


async def _call(model: BaseChatModel, tools: list[BaseTool], prompt: list[BaseMessage], kwargs: dict) -> _Usage:
    try:
        response = await model.bind_tools(tools).ainvoke(prompt, **kwargs)
    except ModelPermissionDeniedError as exc:
        pytest.skip(f"this account can't use the model: {exc}")
    usage = response.usage_metadata or {}
    return _Usage(
        usage.get("input_tokens", 0),
        (usage.get("input_token_details") or {}).get("cache_read") or 0,
        response.response_metadata.get("id") or "",
    )


async def _measure(model: BaseChatModel, prompts: tuple[list[BaseMessage], ...], kwargs: dict) -> list[_Usage]:
    tools = _core_tools()
    return [await _call(model, tools, prompt, kwargs) for prompt in prompts]


def _assert_hits(label: str, usage: list[_Usage]) -> None:
    a, b, c = usage
    logger.info("deferred-cache %s: A=%s B=%s C=%s", label, a, b, c)
    assert b.cache_read >= _HIT_RATIO * a.input, f"{label}: B read {b.cache_read} of A's {a.input} tokens from cache"
    assert c.cache_read >= _HIT_RATIO * b.input, f"{label}: C read {c.cache_read} of B's {b.input} tokens from cache"


@pytest.mark.deferred_cache
@pytest.mark.parametrize("model_spec", DEFERRED_CACHE_INLINE_MODELS)
async def test_inline_definition_keeps_cache(model_spec):
    require_provider_for_model(model_spec)
    model, build_block = _inline_model(model_spec)
    nonce = uuid.uuid4().hex
    kwargs = _cache_kwargs(model, nonce)
    if isinstance(model, ChatAnthropic):
        # A call-time `betas` replaces the model's own list rather than extending it.
        kwargs["betas"] = list(dict.fromkeys([*(model.betas or []), _INLINE_TOOLS_BETA]))
    _assert_hits(f"inline {model_spec}", await _measure(model, _prompts(nonce, build_block(digest_tool())), kwargs))


@pytest.mark.deferred_cache
@pytest.mark.parametrize("model_spec", [s for s in DEFERRED_CACHE_INLINE_MODELS if s.startswith("anthropic:")][:1])
async def test_inline_beta_header_flip_keeps_cache(model_spec):
    require_provider_for_model(model_spec)
    model, build_block = _inline_model(model_spec)
    nonce = uuid.uuid4().hex
    prompts = _prompts(nonce, build_block(digest_tool()))
    _assert_hits(f"inline-flip {model_spec}", await _measure(model, prompts, _cache_kwargs(model, nonce)))


@pytest.mark.deferred_cache
@pytest.mark.parametrize("model_spec", DEFERRED_CACHE_FROZEN_MODELS)
async def test_frozen_array_keeps_cache(model_spec):
    require_provider_for_model(model_spec)
    model = BaseAgent.get_model(model=model_spec)
    nonce = uuid.uuid4().hex
    _assert_hits(f"frozen {model_spec}", await _measure(model, _prompts(nonce), _cache_kwargs(model, nonce)))


@pytest.mark.deferred_cache
@pytest.mark.parametrize("model_spec", [*DEFERRED_FROZEN_ALLOWLISTED, *DEFERRED_FROZEN_CANDIDATES])
async def test_freezing_keeps_more_cache_than_appending(model_spec):
    require_provider_for_model(model_spec)
    model = BaseAgent.get_model(model=model_spec)
    nonce = uuid.uuid4().hex
    kwargs = _cache_kwargs(model, nonce)
    prompt_a, prompt_b, prompt_c = _prompts(nonce)

    a = await _call(model, _core_tools(), prompt_a, kwargs)
    appended = [await _call(model, [*_core_tools(), digest_tool()], prompt, kwargs) for prompt in (prompt_b, prompt_c)]
    frozen = [await _call(model, _core_tools(), prompt, kwargs) for prompt in (prompt_b, prompt_c)]
    logger.info(
        "deferred-cache freeze-vs-append %s: A=%s appended B,C=%s frozen B,C=%s", model_spec, a, appended, frozen
    )

    assert sum(u.cache_read for u in frozen) > sum(u.cache_read for u in appended), (
        f"{model_spec}: freezing kept no more cache than appending over B and C (frozen {frozen}, appended {appended})"
    )
