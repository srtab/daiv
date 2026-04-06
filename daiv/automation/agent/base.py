from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar

from langchain.chat_models import init_chat_model
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph

from core.constants import BOT_NAME
from core.models import ThinkingLevelChoices as ThinkingLevel
from core.site_settings import site_settings

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


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE_GENAI = "google_genai"
    OPENROUTER = "openrouter"

    @staticmethod
    def api_key_field_for(provider: ModelProvider) -> str | None:
        """Return the SiteConfiguration field name that holds the API key for this provider, or None."""
        return _PROVIDER_API_KEY_FIELDS.get(provider)


# Mapping from provider to the SiteConfiguration field that stores its API key.
_PROVIDER_API_KEY_FIELDS: dict[ModelProvider, str] = {
    ModelProvider.ANTHROPIC: "anthropic_api_key",
    ModelProvider.OPENAI: "openai_api_key",
    ModelProvider.GOOGLE_GENAI: "google_api_key",
    ModelProvider.OPENROUTER: "openrouter_api_key",
}


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
        model_kwargs = BaseAgent.get_model_kwargs(
            model=model, model_provider=BaseAgent.get_model_provider(model), thinking_level=thinking_level, **kwargs
        )
        return init_chat_model(**model_kwargs)

    @staticmethod
    def get_model_kwargs(
        *, model_provider: ModelProvider, thinking_level: ThinkingLevel | None = None, **kwargs
    ) -> dict:
        """
        Get the keyword arguments to pass to the model.

        Returns:
            dict: The keyword arguments
        """
        _kwargs = {"temperature": 0, "model_kwargs": {}, "model_provider": model_provider, **kwargs}

        if model_provider == ModelProvider.ANTHROPIC:
            if site_settings.anthropic_api_key is None:
                raise RuntimeError("Anthropic API key is not configured. Set ANTHROPIC_API_KEY or use the config UI.")

            _kwargs["betas"] = ["structured-outputs-2025-11-13"]
            _kwargs["api_key"] = site_settings.anthropic_api_key.get_secret_value()

            if thinking_level and _kwargs["model"].startswith(CLAUDE_THINKING_MODELS):
                max_tokens, thinking_tokens = BaseAgent._get_anthropic_thinking_tokens(
                    thinking_level=thinking_level, max_tokens=kwargs.get("max_tokens", CLAUDE_MAX_TOKENS)
                )
                # When using thinking the temperature need to be set to 1 for Anthropic models
                _kwargs["temperature"] = 1
                _kwargs["max_tokens"] = max_tokens
                _kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_tokens}
            elif "max_tokens" not in _kwargs:
                # As stated in docs: https://docs.anthropic.com/en/api/rate-limits#updated-rate-limits
                # the OTPM is calculated based on the max_tokens. We need to use a fair value to avoid rate limiting.
                # If needed, we can increase this value using the configurable field.
                _kwargs["max_tokens"] = CLAUDE_MAX_TOKENS

        elif model_provider == ModelProvider.OPENAI:
            if site_settings.openai_api_key is None:
                raise RuntimeError("OpenAI API key is not configured. Set OPENAI_API_KEY or use the config UI.")
            _kwargs["api_key"] = site_settings.openai_api_key.get_secret_value()
            _kwargs["use_responses_api"] = True
            if thinking_level and _kwargs["model"].startswith(OPENAI_THINKING_MODELS):
                _kwargs["temperature"] = 1
                _kwargs["reasoning_effort"] = thinking_level

        elif model_provider == ModelProvider.OPENROUTER:
            if site_settings.openrouter_api_key is None:
                raise RuntimeError("OpenRouter API key is not configured. Set OPENROUTER_API_KEY or use the config UI.")
            _kwargs["model"] = _kwargs["model"].split(":", 1)[1]
            # OpenRouter is OpenAI compatible, so we need to use the OpenAI model provider
            _kwargs["model_provider"] = ModelProvider.OPENAI
            _kwargs["model_kwargs"]["extra_headers"] = {
                "HTTP-Referer": "https://srtab.github.io/daiv",
                "X-Title": BOT_NAME,
            }
            _kwargs["openai_api_base"] = site_settings.openrouter_api_base
            _kwargs["openai_api_key"] = site_settings.openrouter_api_key.get_secret_value()

            if thinking_level:
                if _kwargs["model"].startswith(CLAUDE_THINKING_MODELS):
                    max_tokens, thinking_tokens = BaseAgent._get_anthropic_thinking_tokens(
                        thinking_level=thinking_level, max_tokens=_kwargs.get("max_tokens", CLAUDE_MAX_TOKENS)
                    )
                    _kwargs["max_tokens"] = max_tokens
                    _kwargs["extra_body"] = {"reasoning": {"max_tokens": thinking_tokens}}
                    # When using thinking the temperature need to be set to 1 for Anthropic models
                    _kwargs["temperature"] = 1
                else:
                    _kwargs["extra_body"] = {"reasoning": {"effort": thinking_level.value}}

            elif _kwargs["model"].startswith("anthropic") and "max_tokens" not in _kwargs:
                # Avoid rate limiting by setting a fair max_tokens value
                _kwargs["max_tokens"] = CLAUDE_MAX_TOKENS
                _kwargs["model_kwargs"]["extra_headers"]["anthropic-beta"] = "structured-outputs-2025-11-13"

        elif model_provider == ModelProvider.GOOGLE_GENAI:
            if site_settings.google_api_key is None:
                raise RuntimeError("Google API key is not configured. Set GOOGLE_API_KEY or use the config UI.")
            _kwargs["api_key"] = site_settings.google_api_key.get_secret_value()
            _kwargs["include_thoughts"] = True

        return _kwargs

    @staticmethod
    def _get_anthropic_thinking_tokens(*, thinking_level: ThinkingLevel, max_tokens: int) -> tuple[int, int]:
        """
        Get the thinking tokens and max tokens for the model.
        """
        if thinking_level == ThinkingLevel.MINIMAL:
            return max_tokens + 1_024, 1_024
        elif thinking_level == ThinkingLevel.LOW:
            return max_tokens + 4_096, 4_096
        elif thinking_level == ThinkingLevel.MEDIUM:
            return max_tokens + 25_600, 25_600
        elif thinking_level == ThinkingLevel.HIGH:
            return 64_000, 64_000 - max_tokens
        raise ValueError(f"Unsupported thinking level: {thinking_level}")

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
    def get_model_provider(model_name: str) -> ModelProvider:
        """
        Get the model provider.

        Args:
            model_name (str): The model name

        Returns:
            ModelProvider: The model provider
        """
        if any(model_name.startswith(pre) for pre in ("gpt-4", "gpt-5", "o4")):
            return ModelProvider.OPENAI
        elif model_name.startswith("claude"):
            return ModelProvider.ANTHROPIC
        elif model_name.startswith("gemini"):
            return ModelProvider.GOOGLE_GENAI
        elif model_name.startswith("openrouter:"):
            return ModelProvider.OPENROUTER
        else:
            raise ValueError(f"Unknown/Unsupported provider for model {model_name}")
