from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from langchain.chat_models import init_chat_model

from automation.agent.chat_models import ChatOpenRouter
from automation.agent.model_catalog.exceptions import MissingApiKeyError
from automation.agent.model_spec import HTTPX_CLIENT_PROVIDER_TYPES, ResolvedProvider, parse_model_spec
from automation.agent.provider_clients import build_sdk_client_kwargs
from core.constants import BOT_NAME
from core.models import ProviderType
from core.models import ThinkingLevelChoices as ThinkingLevel

logger = logging.getLogger("daiv.automation")

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from core.models import Provider


CLAUDE_MAX_TOKENS = 16_384

# Every thinking-capable Claude model, declared once: native Anthropic name, OpenRouter
# slug, and whether it belongs to the adaptive-thinking generation. Both spellings are
# spelled out rather than derived from each other — a future naming scheme would silently
# mint a wrong slug. Names are matched as prefixes, so dated variants (e.g.
# ``claude-opus-5-20260101``) are covered.
_CLAUDE_MODELS = (
    # (anthropic name, openrouter slug, adaptive thinking)
    ("claude-sonnet-4-5", "anthropic/claude-sonnet-4.5", False),
    ("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6", False),
    ("claude-sonnet-5", "anthropic/claude-sonnet-5", True),
    ("claude-opus-4-5", "anthropic/claude-opus-4.5", False),
    ("claude-opus-4-6", "anthropic/claude-opus-4.6", False),
    ("claude-opus-4-7", "anthropic/claude-opus-4.7", True),
    ("claude-opus-4-8", "anthropic/claude-opus-4.8", True),
    ("claude-opus-5", "anthropic/claude-opus-5", True),
    ("claude-haiku-4-5", "anthropic/claude-haiku-4.5", False),
    ("claude-haiku-5", "anthropic/claude-haiku-5", True),
    ("claude-fable-5", "anthropic/claude-fable-5", True),
)

CLAUDE_THINKING_MODELS = tuple(name for native, slug, _ in _CLAUDE_MODELS for name in (native, slug))

# Opus 4.7 and every later generation removed the manual extended-thinking config:
# ``thinking={"type": "enabled", "budget_tokens": N}`` returns a 400
# ("`thinking.type.enabled` is not supported for this model. Use `thinking.type.adaptive`
# and `output_config.effort` to control thinking behavior."). Thinking depth is driven by
# the effort level instead. Derived from the table so it cannot drift out of
# ``CLAUDE_THINKING_MODELS``: the adaptive branch is only reachable for thinking-capable
# models, so a model listed here but not there would silently get no thinking at all.
CLAUDE_ADAPTIVE_THINKING_MODELS = tuple(
    name for native, slug, adaptive in _CLAUDE_MODELS if adaptive for name in (native, slug)
)

# The same generation also removed the sampling parameters — sending ``temperature``
# returns a 400 ``invalid_request_error``. Dropping sampling params and dropping manual
# thinking are two distinct API rules that currently cover exactly the same models; if
# they ever diverge, give this its own column in ``_CLAUDE_MODELS``.
CLAUDE_NO_TEMPERATURE_MODELS = CLAUDE_ADAPTIVE_THINKING_MODELS

OPENAI_GPT6_MODELS = ("gpt-6", "openai/gpt-6")

OPENAI_THINKING_MODELS = (
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6",
    "openai/gpt-5.2",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.4",
    "openai/gpt-5.5",
    "openai/gpt-5.6",
    *OPENAI_GPT6_MODELS,
)

# GPT-6 rejects ``temperature`` with a 400; langchain-openai only strips it for ``gpt-5*``.
NO_TEMPERATURE_MODELS = (*CLAUDE_NO_TEMPERATURE_MODELS, *OPENAI_GPT6_MODELS)

ANTHROPIC_STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-11-13"


# Shared by Anthropic's ``output_config.effort`` and OpenAI's ``reasoning_effort``: neither
# accepts our ``minimal``, so it down-maps to ``low``. ``max`` is intentionally unused.
_EFFORT_BY_LEVEL = {
    ThinkingLevel.MINIMAL: "low",
    ThinkingLevel.LOW: "low",
    ThinkingLevel.MEDIUM: "medium",
    ThinkingLevel.HIGH: "high",
    ThinkingLevel.XHIGH: "xhigh",
}

# Import-time parity guard, mirroring _OPENROUTER_ANTHROPIC_MAX_TOKENS below: a new
# ThinkingLevel without an effort mapping would crash mid-request with a bare KeyError.
assert set(ThinkingLevel) == _EFFORT_BY_LEVEL.keys(), (
    f"_EFFORT_BY_LEVEL missing entries for {set(ThinkingLevel) - _EFFORT_BY_LEVEL.keys()}"
)


def _anthropic_thinking_tokens(*, thinking_level: ThinkingLevel, max_tokens: int) -> tuple[int, int]:
    if thinking_level == ThinkingLevel.MINIMAL:
        return max_tokens + 1_024, 1_024
    if thinking_level == ThinkingLevel.LOW:
        return max_tokens + 4_096, 4_096
    if thinking_level == ThinkingLevel.MEDIUM:
        return max_tokens + 25_600, 25_600
    if thinking_level in (ThinkingLevel.HIGH, ThinkingLevel.XHIGH):
        # Sonnet/Haiku cap output at 64K without the ``output-128k`` beta. XHIGH and HIGH
        # share that ceiling — they diverge via ``effort`` on the adaptive path and via the
        # OpenRouter budget table, never through max_tokens here.
        return 64_000, 64_000 - max_tokens
    raise ValueError(f"Unsupported thinking level: {thinking_level}")


def _apply_anthropic_thinking(kw: dict, thinking_level: ThinkingLevel | None, model_name: str) -> None:
    if not thinking_level or not model_name.startswith(CLAUDE_THINKING_MODELS):
        # OTPM rate-limits scale with max_tokens (see Anthropic rate-limit docs); set
        # a fair default to avoid throttling. Callers can override via kwargs.
        if "max_tokens" not in kw:
            kw["max_tokens"] = CLAUDE_MAX_TOKENS
        return
    max_tokens, budget = _anthropic_thinking_tokens(
        thinking_level=thinking_level, max_tokens=kw.get("max_tokens", CLAUDE_MAX_TOKENS)
    )
    # Keep the per-level output ceiling on both paths: with adaptive thinking there is no
    # client-side budget, so max_tokens is the only ceiling and thinking is billed against it.
    kw["max_tokens"] = max_tokens

    if model_name.startswith(CLAUDE_ADAPTIVE_THINKING_MODELS):
        # ``display`` must be requested explicitly: it defaults to "omitted" on these
        # models, which streams thinking blocks with empty text, so the reasoning never
        # reaches DAIV at all — live AG-UI events and reloaded transcripts alike end up
        # with nothing to show. ``effort`` is langchain-anthropic's shorthand for
        # ``output_config.effort`` and takes precedence over any output_config we or
        # with_structured_output pass, so it can't be clobbered by the ``format`` key.
        kw["thinking"] = {"type": "adaptive", "display": "summarized"}
        kw["effort"] = _EFFORT_BY_LEVEL[thinking_level]
    else:
        # Anthropic requires temperature=1 when manual thinking is enabled.
        kw["temperature"] = 1
        kw["thinking"] = {"type": "enabled", "budget_tokens": budget}


def _apply_openai_reasoning(kw: dict, thinking_level: ThinkingLevel | None, model_name: str) -> None:
    if thinking_level and model_name.startswith(OPENAI_THINKING_MODELS):
        kw["temperature"] = 1
        kw["reasoning_effort"] = _EFFORT_BY_LEVEL[thinking_level]


# OpenRouter derives ``budget_tokens`` server-side from ``max_tokens`` for Anthropic
# models. Capped at 64K — Sonnet/Haiku reject larger outputs without the ``output-128k`` beta.
_OPENROUTER_ANTHROPIC_MAX_TOKENS = {
    ThinkingLevel.MINIMAL: 10_240,
    ThinkingLevel.LOW: 20_480,
    ThinkingLevel.MEDIUM: 51_200,
    ThinkingLevel.HIGH: 64_000,
    ThinkingLevel.XHIGH: 64_000,
}

# Import-time parity guard: every ThinkingLevel must have a max_tokens entry, or
# OpenRouter Anthropic requests would crash with a bare KeyError mid-request when
# a new level is added without updating the table.
assert set(ThinkingLevel) == _OPENROUTER_ANTHROPIC_MAX_TOKENS.keys(), (
    f"_OPENROUTER_ANTHROPIC_MAX_TOKENS missing entries for "
    f"{set(ThinkingLevel) - _OPENROUTER_ANTHROPIC_MAX_TOKENS.keys()}"
)


def _apply_openrouter_thinking(kw: dict, thinking_level: ThinkingLevel | None, model_name: str) -> None:
    if not thinking_level:
        if model_name.startswith("anthropic") and "max_tokens" not in kw:
            kw["max_tokens"] = CLAUDE_MAX_TOKENS
            kw["model_kwargs"].setdefault("extra_headers", {})["anthropic-beta"] = ANTHROPIC_STRUCTURED_OUTPUTS_BETA
        return
    # ``enabled: true`` is the universal switch on OpenRouter; some providers
    # (notably z.ai's GLM family) ignore ``effort`` and require the explicit flag.
    # OpenRouter converts ``effort`` to ``budget_tokens`` server-side for Anthropic
    # models, so we no longer compute the budget ourselves on this path.
    kw["extra_body"]["reasoning"] = {"enabled": True, "effort": thinking_level}
    kw["temperature"] = 1
    if model_name.startswith(CLAUDE_THINKING_MODELS):
        # Anthropic requires temperature=1 when thinking is enabled; OpenRouter
        # passes the kwarg through to the upstream Anthropic API. Models that reject
        # sampling params get it stripped again by _apply_temperature_support.
        kw["temperature"] = 1
        kw["max_tokens"] = _OPENROUTER_ANTHROPIC_MAX_TOKENS[thinking_level]


def _apply_temperature_support(kw: dict, model_name: str) -> None:
    """Drop ``temperature`` for models that no longer accept it.

    The base kwargs always seed ``temperature`` (and the thinking helpers may override
    it to ``1``), but Opus 4.7+ and GPT-6 removed the sampling parameters and reject any
    request that includes them. Run this last so it wins over every provider/thinking
    branch and any caller-supplied ``temperature``."""
    if model_name.startswith(NO_TEMPERATURE_MODELS):
        kw.pop("temperature", None)


_PENDING_ACLOSE_TASKS: set[asyncio.Task] = set()


def _close_insecure_http_clients(kw: dict) -> None:
    """Close httpx clients attached by :func:`_apply_insecure_http_clients` when the
    downstream :func:`init_chat_model` call fails — prevents ``ResourceWarning`` and
    connection-pool retention under retry loops."""
    sync_client = kw.pop("http_client", None)
    async_client = kw.pop("http_async_client", None)

    if sync_client is not None:
        # The sync transport can raise on already-closed pools / interpreter shutdown
        # — suppress so we don't mask the original init_chat_model exception.
        with contextlib.suppress(Exception):
            sync_client.close()

    if async_client is None:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — we cannot await aclose() from sync context. Surface this
        # so operators can correlate ResourceWarnings with the call site.
        logger.warning(
            "Cannot close insecure httpx.AsyncClient: no running event loop. "
            "Expect a ResourceWarning; the underlying connection pool will be released by GC."
        )
        return

    # Retain a reference until done so Python's GC doesn't drop the task before
    # aclose() completes (RUF006 / fire-and-forget asyncio task pitfall).
    task = loop.create_task(async_client.aclose())
    _PENDING_ACLOSE_TASKS.add(task)
    task.add_done_callback(_PENDING_ACLOSE_TASKS.discard)


def _apply_insecure_http_clients(kw: dict, row: Provider.Cached) -> None:
    if row.provider_type not in HTTPX_CLIENT_PROVIDER_TYPES:
        logger.warning(
            "Provider %r: verify_ssl=False ignored for provider_type %s (SDK has no"
            " http_client hook); mount the CA into the container.",
            row.slug,
            row.provider_type,
        )
        return
    import httpx

    kw["http_client"] = httpx.Client(verify=False)  # noqa: S501  # admin-opted-in via Provider.verify_ssl
    kw["http_async_client"] = httpx.AsyncClient(verify=False)  # noqa: S501


def get_model(*, model: str, thinking_level: ThinkingLevel | None = None, **kwargs) -> BaseChatModel:
    """
    Get the model instance to use for the agent.

    Returns:
        BaseChatModel: The model instance
    """
    resolved = parse_model_spec(model)
    model_kwargs = get_model_kwargs(resolved=resolved, thinking_level=thinking_level, **kwargs)
    try:
        if resolved.row.provider_type == ProviderType.OPENROUTER:
            # OpenRouter routes through our ChatOpenAI subclass (reasoning extraction +
            # base-URL default). init_chat_model can't return a subclass, so instantiate
            # directly.
            return ChatOpenRouter(**model_kwargs)
        return init_chat_model(**model_kwargs)
    except Exception:
        _close_insecure_http_clients(model_kwargs)
        raise


def get_model_kwargs(*, resolved: ResolvedProvider, thinking_level: ThinkingLevel | None = None, **kwargs) -> dict:
    """
    Get the keyword arguments to pass to ``init_chat_model`` for the given
    resolved provider row.
    """
    row = resolved.row
    if not row.is_enabled:
        raise RuntimeError(f"Provider '{row.slug}' is disabled. Enable it in the configuration.")

    # Shared primitive: api_key plaintext, base_url, default_headers.
    # LangChain needs both sync and async httpx clients when verify_ssl=False
    # — those are constructed below via _apply_insecure_http_clients, so we
    # skip the helper's AsyncClient here to avoid leaking it.
    try:
        sdk_kw = build_sdk_client_kwargs(row, with_http_client=False)
    except MissingApiKeyError as err:
        raise RuntimeError(f"Provider '{row.slug}' has no API key configured.") from err

    kw: dict = {"temperature": 0, "model_kwargs": {}, "model": resolved.model_name, **kwargs}
    kw["api_key"] = sdk_kw["api_key"]

    if row.provider_type == ProviderType.ANTHROPIC:
        kw["model_provider"] = ProviderType.ANTHROPIC.value
        kw["betas"] = [ANTHROPIC_STRUCTURED_OUTPUTS_BETA]
        if sdk_kw["base_url"]:
            kw["base_url"] = sdk_kw["base_url"]
        _apply_anthropic_thinking(kw, thinking_level, resolved.model_name)

    elif row.provider_type == ProviderType.OPENAI:
        kw["model_provider"] = ProviderType.OPENAI.value
        if row.use_responses_api:
            kw["use_responses_api"] = True
        if sdk_kw["base_url"]:
            kw["openai_api_base"] = sdk_kw["base_url"]
        _apply_openai_reasoning(kw, thinking_level, resolved.model_name)

    elif row.provider_type == ProviderType.OPENROUTER:
        # OpenRouter is OpenAI-compatible over the wire, but constructed directly as a
        # ChatOpenRouter (not via init_chat_model) — that subclass supplies the base-URL
        # default, so mirror the OpenAI branch and only forward an explicit override.
        if sdk_kw["base_url"]:
            kw["openai_api_base"] = sdk_kw["base_url"]
        kw["model_kwargs"]["extra_headers"] = {"HTTP-Referer": "https://srtab.github.io/daiv", "X-Title": BOT_NAME}
        # Unsorted, the pool includes open-weight models' priority endpoints (~2x the price), and
        # cache-sticky routing can keep a whole run on one.
        kw["extra_body"] = {"provider": {"sort": "price"}}
        _apply_openrouter_thinking(kw, thinking_level, resolved.model_name)

    elif row.provider_type == ProviderType.GOOGLE_GENAI:
        kw["model_provider"] = ProviderType.GOOGLE_GENAI.value
        kw["include_thoughts"] = True
        if sdk_kw["base_url"]:
            kw["base_url"] = sdk_kw["base_url"]

    else:
        raise RuntimeError(f"Unknown provider_type {row.provider_type!r} on slug {row.slug!r}")

    if not row.verify_ssl:
        _apply_insecure_http_clients(kw, row)

    if sdk_kw["default_headers"]:
        # User-supplied headers don't override agent-managed ones (HTTP-Referer,
        # X-Title, anthropic-beta); admins customise via the agent code, not the row.
        existing = kw["model_kwargs"].setdefault("extra_headers", {})
        for name, value in sdk_kw["default_headers"].items():
            existing.setdefault(name, value)

    _apply_temperature_support(kw, resolved.model_name)

    return kw
