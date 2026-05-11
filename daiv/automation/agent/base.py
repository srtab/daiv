from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph

from core.constants import BOT_NAME
from core.models import Provider, ProviderType
from core.models import ThinkingLevelChoices as ThinkingLevel

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


def _anthropic_thinking_tokens(*, thinking_level: ThinkingLevel, max_tokens: int) -> tuple[int, int]:
    if thinking_level == ThinkingLevel.MINIMAL:
        return max_tokens + 1_024, 1_024
    if thinking_level == ThinkingLevel.LOW:
        return max_tokens + 4_096, 4_096
    if thinking_level == ThinkingLevel.MEDIUM:
        return max_tokens + 25_600, 25_600
    if thinking_level == ThinkingLevel.HIGH:
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
        kw["reasoning_effort"] = thinking_level


def _apply_openrouter_thinking(kw: dict, thinking_level: ThinkingLevel | None, model_name: str) -> None:
    if not thinking_level:
        if model_name.startswith("anthropic") and "max_tokens" not in kw:
            kw["max_tokens"] = CLAUDE_MAX_TOKENS
            kw["model_kwargs"]["extra_headers"]["anthropic-beta"] = "structured-outputs-2025-11-13"
        return
    if model_name.startswith(CLAUDE_THINKING_MODELS):
        max_tokens, budget = _anthropic_thinking_tokens(
            thinking_level=thinking_level, max_tokens=kw.get("max_tokens", CLAUDE_MAX_TOKENS)
        )
        kw["max_tokens"] = max_tokens
        kw["extra_body"] = {"reasoning": {"max_tokens": budget}}
        kw["temperature"] = 1
    else:
        # ``enabled: true`` is the universal switch on OpenRouter; some providers
        # (notably z.ai's GLM family) ignore ``effort`` and require the explicit flag.
        kw["extra_body"] = {"reasoning": {"enabled": True, "effort": thinking_level}}


@dataclass(frozen=True)
class ResolvedProvider:
    """Result of resolving a ``slug:model_name`` string against the Provider table."""

    row: Provider.Cached
    model_name: str


_BARE_NAME_HEURISTICS = (
    (("gpt-4", "gpt-5", "o4"), "openai"),
    (("claude",), "anthropic"),
    (("gemini",), "google_genai"),
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
        return init_chat_model(**model_kwargs)

    @staticmethod
    def get_model_kwargs(*, resolved: ResolvedProvider, thinking_level: ThinkingLevel | None = None, **kwargs) -> dict:
        """
        Get the keyword arguments to pass to ``init_chat_model`` for the given
        resolved provider row.
        """
        row = resolved.row
        if not row.is_enabled:
            raise RuntimeError(f"Provider '{row.slug}' is disabled. Enable it in the configuration.")
        if row.api_key is None:
            raise RuntimeError(f"Provider '{row.slug}' has no API key configured.")

        kw: dict = {"temperature": 0, "model_kwargs": {}, "model": resolved.model_name, **kwargs}

        if row.provider_type == ProviderType.ANTHROPIC:
            kw["model_provider"] = ProviderType.ANTHROPIC.value
            kw["api_key"] = row.api_key.get_secret_value()
            kw["betas"] = ["structured-outputs-2025-11-13"]
            if row.base_url:
                kw["base_url"] = row.base_url
            _apply_anthropic_thinking(kw, thinking_level, resolved.model_name)

        elif row.provider_type == ProviderType.OPENAI:
            kw["model_provider"] = ProviderType.OPENAI.value
            kw["api_key"] = row.api_key.get_secret_value()
            kw["use_responses_api"] = True
            if row.base_url:
                kw["openai_api_base"] = row.base_url
            _apply_openai_reasoning(kw, thinking_level, resolved.model_name)

        elif row.provider_type == ProviderType.OPENROUTER:
            # OpenRouter is OpenAI-compatible over the wire.
            kw["model_provider"] = ProviderType.OPENAI.value
            kw["openai_api_base"] = row.base_url or "https://openrouter.ai/api/v1"
            kw["openai_api_key"] = row.api_key.get_secret_value()
            kw["model_kwargs"]["extra_headers"] = {"HTTP-Referer": "https://srtab.github.io/daiv", "X-Title": BOT_NAME}
            _apply_openrouter_thinking(kw, thinking_level, resolved.model_name)

        elif row.provider_type == ProviderType.GOOGLE_GENAI:
            kw["model_provider"] = ProviderType.GOOGLE_GENAI.value
            kw["api_key"] = row.api_key.get_secret_value()
            kw["include_thoughts"] = True
            if row.base_url:
                kw["base_url"] = row.base_url

        else:
            raise RuntimeError(f"Unknown provider_type {row.provider_type!r} on slug {row.slug!r}")

        if row.extra_headers:
            kw["model_kwargs"].setdefault("extra_headers", {}).update(row.extra_headers)

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
