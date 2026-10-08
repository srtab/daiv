"""Live probe: does each deferred-tool delivery mode keep the provider's prompt cache across a tool load?

Excluded from ``make test``. Each case replays three scripted model calls, each a strict prefix of the next:

  * A: an ~8k-token system prompt and the user's request. Writes the cache.
  * B: A plus the ``tool_search`` call and its result, with the loaded tool delivered the case's way.
  * C: B plus a call to the loaded tool and its result.

A hit means B reads A's prompt from the cache and C reads B's. The ``tool_search`` result embeds the schema in
every case, as production does. Cases:

  * inline: the tool is declared mid-conversation right after the ``tool_search`` result (Anthropic
    ``tool_addition``, OpenAI Responses ``additional_tools``); on Anthropic the ``inline-tools`` beta header rides
    on every call.
  * inline header flip (Anthropic only): the beta header first appears on B, as langchain-anthropic adds it only once
    a tool block is in the request. Anthropic doesn't document whether that resets the cache; it must not.
  * frozen: the tools array never changes; the schema reaches the model only through the ``tool_search`` result.
  * append control: the tool joins the tools array on B, which must miss while the frozen B still hits.

Run with live logs to see every call's numbers::

    uv run pytest --envfile +docker/local/app/config.secrets.env --no-cov --log-cli-level=INFO \\
        tests/integration_tests/test_deferred_tools_cache.py -m deferred_cache

Models come from ``DAIV_EVAL_DEFERRED_CACHE_INLINE_MODELS`` and ``DAIV_EVAL_DEFERRED_CACHE_FROZEN_MODELS``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, NamedTuple

import pytest
from langchain_anthropic import ChatAnthropic, convert_to_anthropic_tool
from langchain_anthropic.chat_models import _INLINE_TOOLS_BETA, _supports_mid_conversation_system_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openai import ChatOpenAI

from automation.agent import BaseAgent
from automation.agent.chat_models import ChatOpenRouter
from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block
from automation.agent.deferred.search_tool import TOOL_SEARCH_NAME, make_tool_search
from core.constants import ModelName

from .deferred_tools import TOOL_NAME, digest_tool, tool_search_result
from .utils import DEFERRED_CACHE_FROZEN_MODELS, DEFERRED_CACHE_INLINE_MODELS, require_provider_for_model

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
_APPEND_CONTROL_MODEL = ModelName.CLAUDE_SONNET_4_6


class _Usage(NamedTuple):
    input: int
    cache_read: int


def _tool_search() -> BaseTool:
    return make_tool_search(lambda: DeferredToolsIndex([digest_tool()]), top_k_default=5, top_k_max=10)


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
    system = SystemMessage(
        content=f"Session {nonce}.\n\n{_FILLER}\n\n{build_deferred_tools_block(DeferredToolsIndex([tool]))}"
    )
    request = HumanMessage(content="Fetch a digest of ticket ABC-123, at most 3 notes, and skip resolved children.")
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

    prompt_a = [system, request]
    prompt_b = [*prompt_a, search_call, search_result, *([SystemMessage(content=[delivery])] if delivery else [])]
    prompt_c = [*prompt_b, digest_call, digest_result]
    return prompt_a, prompt_b, prompt_c


async def _call(model: BaseChatModel, tools: list[BaseTool], prompt: list[BaseMessage], kwargs: dict) -> _Usage:
    response = await model.bind_tools(tools).ainvoke(prompt, **kwargs)
    usage = response.usage_metadata or {}
    return _Usage(usage.get("input_tokens", 0), (usage.get("input_token_details") or {}).get("cache_read") or 0)


async def _measure(model: BaseChatModel, prompts: tuple[list[BaseMessage], ...], kwargs: dict) -> list[_Usage]:
    tools = [_tool_search()]
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
@pytest.mark.parametrize("model_spec", DEFERRED_CACHE_INLINE_MODELS)
async def test_inline_beta_header_flip_keeps_cache(model_spec):
    require_provider_for_model(model_spec)
    model, build_block = _inline_model(model_spec)
    if not isinstance(model, ChatAnthropic):
        pytest.skip("only Anthropic gates inline definitions behind a beta header")
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
async def test_append_control_misses_cache():
    require_provider_for_model(_APPEND_CONTROL_MODEL)
    model = BaseAgent.get_model(model=_APPEND_CONTROL_MODEL)
    nonce = uuid.uuid4().hex
    kwargs = _cache_kwargs(model, nonce)
    prompt_a, prompt_b, _ = _prompts(nonce)

    a = await _call(model, [_tool_search()], prompt_a, kwargs)
    appended = await _call(model, [_tool_search(), digest_tool()], prompt_b, kwargs)
    frozen = await _call(model, [_tool_search()], prompt_b, kwargs)
    logger.info("deferred-cache append-control: A=%s appended B=%s frozen B=%s", a, appended, frozen)

    assert frozen.cache_read >= _HIT_RATIO * a.input, f"A's cache was never written: frozen B read {frozen}"
    assert appended.cache_read < _HIT_RATIO * a.input, f"Control violated: appended B still read {appended}"
