from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, NamedTuple

from langchain.chat_models import init_chat_model

from automation.agent.chat_models import ChatOpenRouter
from automation.agent.model_spec import HTTPX_CLIENT_PROVIDER_TYPES, ResolvedProvider, parse_model_spec
from automation.agent.provider_clients import MissingApiKeyError, build_sdk_client_kwargs
from core.constants import BOT_NAME
from core.models import ProviderType
from core.models import ThinkingLevelChoices as ThinkingLevel
from core.site_settings import site_settings

logger = logging.getLogger("daiv.automation")

if TYPE_CHECKING:
    from collections.abc import Iterable

    import openai
    from langchain_core.language_models.chat_models import BaseChatModel

    from core.models import Provider


CLAUDE_MAX_TOKENS = 16_384


class _ClaudeModel(NamedTuple):
    native: str
    slug: str
    adaptive: bool = True  # 4.6 deprecates ``budget_tokens`` for adaptive thinking; 4.7+ rejects it
    no_sampling: bool = True
    xhigh: bool = True


# Both spellings are spelled out (deriving one would mint wrong slugs) and matched as prefixes,
# so dated variants are covered. The defaults describe the 4.7+ generation.
_CLAUDE_MODELS = (
    _ClaudeModel("claude-sonnet-4-5", "anthropic/claude-sonnet-4.5", adaptive=False, no_sampling=False, xhigh=False),
    _ClaudeModel("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6", no_sampling=False, xhigh=False),
    _ClaudeModel("claude-sonnet-5", "anthropic/claude-sonnet-5"),
    _ClaudeModel("claude-opus-4-5", "anthropic/claude-opus-4.5", adaptive=False, no_sampling=False, xhigh=False),
    _ClaudeModel("claude-opus-4-6", "anthropic/claude-opus-4.6", no_sampling=False, xhigh=False),
    _ClaudeModel("claude-opus-4-7", "anthropic/claude-opus-4.7"),
    _ClaudeModel("claude-opus-4-8", "anthropic/claude-opus-4.8"),
    _ClaudeModel("claude-opus-5", "anthropic/claude-opus-5"),
    _ClaudeModel("claude-haiku-4-5", "anthropic/claude-haiku-4.5", adaptive=False, no_sampling=False, xhigh=False),
    _ClaudeModel("claude-haiku-5", "anthropic/claude-haiku-5"),
    _ClaudeModel("claude-fable-5", "anthropic/claude-fable-5"),
)


def _claude_names(models: Iterable[_ClaudeModel]) -> tuple[str, ...]:
    return tuple(name for m in models for name in (m.native, m.slug))


CLAUDE_THINKING_MODELS = _claude_names(_CLAUDE_MODELS)
CLAUDE_ADAPTIVE_THINKING_MODELS = _claude_names(m for m in _CLAUDE_MODELS if m.adaptive)
CLAUDE_NO_TEMPERATURE_MODELS = _claude_names(m for m in _CLAUDE_MODELS if m.no_sampling)
CLAUDE_XHIGH_EFFORT_MODELS = _claude_names(m for m in _CLAUDE_MODELS if m.xhigh)

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
# accepts our ``minimal``, so it down-maps to ``low``.
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
    # Anthropic requires temperature=1 while thinking; _apply_temperature_support strips it on 4.7+.
    kw["temperature"] = 1

    if model_name.startswith(CLAUDE_ADAPTIVE_THINKING_MODELS):
        # ``display`` defaults to "omitted" on 4.7+, leaving DAIV no reasoning to show. ``effort``
        # (langchain's ``output_config.effort`` shorthand) wins over with_structured_output's ``format``.
        kw["thinking"] = {"type": "adaptive", "display": "summarized"}
        effort = _EFFORT_BY_LEVEL[thinking_level]
        if effort == "xhigh" and not model_name.startswith(CLAUDE_XHIGH_EFFORT_MODELS):
            effort = "max"  # 4.6 has no ``xhigh``; ``max`` is its only level above ``high``
        kw["effort"] = effort
    else:
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


def _resolve_request_timeout(provider_type: ProviderType, timeout_seconds: float) -> openai.Timeout | float:
    """Return a per-request timeout in the shape each provider's langchain integration accepts.

    ``langchain-openai`` (OpenAI + OpenRouter) takes the SDK's own ``Timeout``, which gives the
    connect phase a short fuse. Being unhashable, it also keeps langchain from handing every model
    one process-wide async client, whose pooled connections die with the event loop of the task
    that opened them. ``langchain-anthropic`` and ``langchain-google-genai`` only accept a number.
    """
    timeout_seconds = float(timeout_seconds)
    if provider_type in HTTPX_CLIENT_PROVIDER_TYPES:
        import openai

        return openai.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
    return timeout_seconds


def _apply_request_timeout_and_retries(kw: dict, provider_type: ProviderType) -> None:
    """Bound how long a model call can hang and how many times it retries.

    Unset, every provider is unbounded: langchain passes ``timeout=None`` straight to each SDK.
    A caller's own ``timeout`` / ``max_retries`` win, but an explicit ``None`` counts as unset so
    no caller can reintroduce the unbounded default. A caller's numeric timeout is shaped like the
    default, so it cannot opt an OpenAI-family model back into the process-wide client.
    """
    if kw.get("max_retries") is None:
        kw["max_retries"] = site_settings.model_max_retries
    timeout = kw.get("timeout")
    if timeout is None:
        timeout = site_settings.model_request_timeout_seconds
    if isinstance(timeout, int | float):
        kw["timeout"] = _resolve_request_timeout(provider_type, timeout)


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

    _apply_request_timeout_and_retries(kw, row.provider_type)

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
