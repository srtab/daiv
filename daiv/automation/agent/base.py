from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph

from automation.agent.model_catalog.exceptions import MissingApiKeyError
from automation.agent.provider_clients import build_sdk_client_kwargs
from core.constants import BOT_NAME
from core.models import Provider, ProviderType
from core.models import ThinkingLevelChoices as ThinkingLevel

logger = logging.getLogger("daiv.automation")

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


CLAUDE_MAX_TOKENS = 16_384

CLAUDE_THINKING_MODELS = (
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.5",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-haiku-4.5",
)

OPENAI_THINKING_MODELS = ("gpt-5.2", "gpt-5.3-codex", "openai/gpt-5.2", "openai/gpt-5.3-codex")

ANTHROPIC_STRUCTURED_OUTPUTS_BETA = "structured-outputs-2025-11-13"


def _anthropic_thinking_tokens(*, thinking_level: ThinkingLevel, max_tokens: int) -> tuple[int, int]:
    if thinking_level == ThinkingLevel.MINIMAL:
        return max_tokens + 1_024, 1_024
    if thinking_level == ThinkingLevel.LOW:
        return max_tokens + 4_096, 4_096
    if thinking_level == ThinkingLevel.MEDIUM:
        return max_tokens + 25_600, 25_600
    if thinking_level in (ThinkingLevel.HIGH, ThinkingLevel.XHIGH):
        # Sonnet/Haiku cap output at 64K without the ``output-128k`` beta; XHIGH
        # differentiates from HIGH on OpenRouter, not here.
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
    # Anthropic requires temperature=1 when thinking is enabled.
    kw["temperature"] = 1
    kw["max_tokens"] = max_tokens
    kw["thinking"] = {"type": "enabled", "budget_tokens": budget}


def _apply_openai_reasoning(kw: dict, thinking_level: ThinkingLevel | None, model_name: str) -> None:
    if thinking_level and model_name.startswith(OPENAI_THINKING_MODELS):
        kw["temperature"] = 1
        # OpenAI's native ``reasoning_effort`` accepts minimal/low/medium/high but
        # not xhigh — downmap to high, matching OpenRouter's documented Gemini
        # behaviour for the same level.
        kw["reasoning_effort"] = ThinkingLevel.HIGH if thinking_level == ThinkingLevel.XHIGH else thinking_level


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
    kw["extra_body"] = {"reasoning": {"enabled": True, "effort": thinking_level}}
    if model_name.startswith(CLAUDE_THINKING_MODELS):
        # Anthropic requires temperature=1 when thinking is enabled; OpenRouter
        # passes the kwarg through to the upstream Anthropic API.
        kw["temperature"] = 1
        kw["max_tokens"] = _OPENROUTER_ANTHROPIC_MAX_TOKENS[thinking_level]


@dataclass(frozen=True)
class ResolvedProvider:
    """Result of resolving a ``slug:model_name`` string against the Provider table."""

    row: Provider.Cached
    model_name: str


# langchain-anthropic and langchain-google-genai don't expose ``http_client`` /
# ``http_async_client`` as model fields, so ``verify_ssl=False`` can't reach the
# underlying SDK there. Both fall back to the warn-and-skip branch.
_HTTPX_CLIENT_PROVIDER_TYPES = frozenset({ProviderType.OPENAI, ProviderType.OPENROUTER})


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
    if row.provider_type not in _HTTPX_CLIENT_PROVIDER_TYPES:
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


_BARE_NAME_HEURISTICS = (
    (("gpt-4", "gpt-5", "o4"), ProviderType.OPENAI.value),
    (("claude",), ProviderType.ANTHROPIC.value),
    (("gemini",), ProviderType.GOOGLE_GENAI.value),
)


def parse_model_spec(model_spec: str) -> ResolvedProvider:
    """
    Parse ``slug:model_name`` against the current Provider rows.

    Order:
      1. Explicit ``slug:model`` where ``slug`` matches a Provider row.
      2. Explicit ``google:model`` alias → ``google_genai`` row.
      3. Bare ``model`` matched by prefix heuristic.

    Raises ``ValueError`` on unknown prefix, empty model name, or missing row.
    """
    rows_by_slug = Provider.get_cached_by_slug()

    if ":" in model_spec:
        prefix, model_name = model_spec.split(":", 1)
        if not model_name.strip():
            raise ValueError(f"Empty model name in spec '{model_spec}'")
        if prefix == "google" and "google_genai" in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug["google_genai"], model_name=model_name)
        if prefix in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug[prefix], model_name=model_name)
        raise ValueError(
            f"Unknown provider prefix '{prefix}' in model spec '{model_spec}'. "
            f"Valid prefixes: {', '.join(sorted(rows_by_slug)) or '(no providers configured)'}"
        )

    for prefixes, target_slug in _BARE_NAME_HEURISTICS:
        if model_spec.startswith(prefixes) and target_slug in rows_by_slug:
            return ResolvedProvider(row=rows_by_slug[target_slug], model_name=model_spec)

    raise ValueError(f"Unknown/Unsupported provider for model {model_spec}")


T = TypeVar("T", bound=Runnable)


class BaseAgent(ABC, Generic[T]):  # noqa: UP046
    """
    Base agent class for creating agents that interact with a model.
    """

    _runnable: T
    """
    The runnable instance that can be used to invoke the agent.
    """

    def __init__(self, *, checkpointer: BaseCheckpointSaver | None = None, store: BaseStore | None = None):
        self.checkpointer = checkpointer
        self.store = store

    @classmethod
    async def get_runnable(cls, *args, **kwargs) -> T:
        """
        Get the compiled agent instance.
        """
        instance = cls(*args, **kwargs)
        instance._runnable = await instance.compile()
        return instance._runnable

    @abstractmethod
    async def compile(self) -> T:
        """
        Compile the agent.

        Typically this method returns a Runnable or a CompiledStateGraph.
        """
        pass

    @staticmethod
    def get_model(*, model: str, thinking_level: ThinkingLevel | None = None, **kwargs) -> BaseChatModel:
        """
        Get the model instance to use for the agent.

        Returns:
            BaseChatModel: The model instance
        """
        resolved = parse_model_spec(model)
        model_kwargs = BaseAgent.get_model_kwargs(resolved=resolved, thinking_level=thinking_level, **kwargs)
        try:
            return init_chat_model(**model_kwargs)
        except Exception:
            _close_insecure_http_clients(model_kwargs)
            raise

    @staticmethod
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
            # OpenRouter is OpenAI-compatible over the wire.
            kw["model_provider"] = ProviderType.OPENAI.value
            kw["openai_api_base"] = sdk_kw["base_url"] or "https://openrouter.ai/api/v1"
            kw["model_kwargs"]["extra_headers"] = {"HTTP-Referer": "https://srtab.github.io/daiv", "X-Title": BOT_NAME}
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

        return kw

    async def draw_mermaid_png(self) -> bytes:
        """
        Draw the graph as a Mermaid PNG image.

        Returns:
            The PNG image bytes.
        """
        if isinstance(self._runnable, CompiledStateGraph):
            return (await self._runnable.aget_graph(xray=True)).draw_mermaid_png()
        return (await self._runnable.aget_graph()).draw_mermaid_png()

    def get_num_tokens_from_messages(self, messages: list[BaseMessage], model_name: str) -> int:
        """
        Get the number of tokens from a list of messages.

        Args:
            messages (list[BaseMessage]): The messages
            model_name (str): The model name

        Returns:
            int: The number of tokens
        """
        return BaseAgent.get_model(model=model_name).get_num_tokens_from_messages(messages)

    @staticmethod
    def get_model_provider(model_name: str) -> ProviderType:
        """
        Get the model provider for a model spec.

        Args:
            model_name: The model specification string (e.g. ``"anthropic:claude-sonnet-4-6"``).
        """
        return ProviderType(parse_model_spec(model_name).row.provider_type)
