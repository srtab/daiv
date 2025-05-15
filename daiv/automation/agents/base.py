from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from langchain.chat_models.base import init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph

from automation.conf import settings
from core.constants import BOT_NAME

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_openai.chat_models import ChatOpenAI
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE_GENAI = "google_genai"
    OPENROUTER = "openrouter"


class ThinkingLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


T = TypeVar("T", bound=Runnable)


class BaseAgent(ABC, Generic[T]):  # noqa: UP046
    """
    Base agent class for creating agents that interact with a model.
    """

    def __init__(
        self,
        *,
        usage_handler: OpenAICallbackHandler | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        store: BaseStore | None = None,
    ):
        self.usage_handler = usage_handler or OpenAICallbackHandler()
        self.checkpointer = checkpointer
        self.store = store

    @cached_property
    def agent(self) -> T:
        """
        The compiled agent.
        """
        return self.compile()

    @abstractmethod
    def compile(self) -> T:
        """
        Compile the agent.

        Tipically this method returns a Runnable or a CompiledGraph.
        """
        pass

    async def acompile(self) -> T:
        """
        Asynchronously support to compile.
        """
        raise NotImplementedError("This method should be implemented by the subclass.")

    def get_model(self, *, model: str, thinking_level: ThinkingLevel | None = None, **kwargs) -> BaseChatModel:
        """
        Get the model instance to use for the agent.

        Returns:
            BaseChatModel: The model instance
        """
        model_kwargs = self.get_model_kwargs(
            model=model, model_provider=BaseAgent.get_model_provider(model), thinking_level=thinking_level, **kwargs
        )
        return init_chat_model(**model_kwargs)

    def get_model_kwargs(
        self, *, model_provider: ModelProvider, thinking_level: ThinkingLevel | None = None, **kwargs
    ) -> dict:
        """
        Get the keyword arguments to pass to the model.

        Returns:
            dict: The keyword arguments
        """
        _kwargs = {
            "temperature": 0,
            "callbacks": [self.usage_handler],
            "configurable_fields": ("temperature", "max_tokens"),
            "model_kwargs": {},
            "model_provider": model_provider,
            **kwargs,
        }

        if model_provider == ModelProvider.ANTHROPIC:
            assert settings.ANTHROPIC_API_KEY is not None, "Anthropic API key is not set"
            _kwargs["api_key"] = settings.ANTHROPIC_API_KEY.get_secret_value()
            if thinking_level and _kwargs["model"].startswith("claude-3-7-sonnet"):
                max_tokens, thinking_tokens = self._get_anthropic_thinking_tokens(thinking_level=thinking_level)
                # When using thinking the temperature need to be set to 1
                _kwargs["temperature"] = 1
                _kwargs["max_tokens"] = max_tokens
                _kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_tokens}
            else:
                # As stated in docs: https://docs.anthropic.com/en/api/rate-limits#updated-rate-limits
                # the OTPM is calculated based on the max_tokens. We need to use a fair value to avoid rate limiting.
                # If needed, we can increase this value using the configurable field.
                _kwargs["max_tokens"] = 4_096

            if _kwargs["model"].startswith("claude-3-7-sonnet"):
                # Enable token efficient tools to reduce the number of tokens used and
                # turn more likely parallel tool calls.
                _kwargs["model_kwargs"]["extra_headers"] = {"anthropic-beta": "token-efficient-tools-2025-02-19"}
        elif model_provider == ModelProvider.OPENAI:
            assert settings.OPENAI_API_KEY is not None, "OpenAI API key is not set"
            _kwargs["api_key"] = settings.OPENAI_API_KEY.get_secret_value()
            if thinking_level and _kwargs["model"].startswith(("o1", "o3")):
                _kwargs["temperature"] = 1
                _kwargs["reasoning_effort"] = thinking_level

        elif model_provider == ModelProvider.OPENROUTER:
            assert settings.OPENROUTER_API_KEY is not None, "OpenRouter API key is not set"
            _kwargs["model"] = _kwargs["model"].split(":", 1)[1]
            # OpenRouter is OpenAI compatible, so we need to use the OpenAI model provider
            _kwargs["model_provider"] = ModelProvider.OPENAI
            _kwargs["model_kwargs"]["extra_headers"] = {
                "HTTP-Referer": "https://srtab.github.io/daiv",
                "X-Title": BOT_NAME,
            }
            _kwargs["openai_api_base"] = settings.OPENROUTER_API_BASE
            _kwargs["openai_api_key"] = settings.OPENROUTER_API_KEY.get_secret_value()

            if _kwargs["model"].startswith("anthropic/claude-3-7-sonnet"):
                _kwargs["model_kwargs"]["extra_headers"]["anthropic-beta"] = "token-efficient-tools-2025-02-19"

            if thinking_level:
                _kwargs["temperature"] = 1
                _kwargs["extra_body"] = {"reasoning": {"effort": thinking_level}}

            elif _kwargs["model"].startswith("anthropic"):
                # Avoid rate limiting by setting a fair max_tokens value
                _kwargs["max_tokens"] = 4_096

        elif model_provider == ModelProvider.GOOGLE_GENAI:
            assert settings.GOOGLE_API_KEY is not None, "Google API key is not set"
            _kwargs["api_key"] = settings.GOOGLE_API_KEY.get_secret_value()

        return _kwargs

    def _get_anthropic_thinking_tokens(self, *, thinking_level: ThinkingLevel) -> tuple[int, dict]:
        """
        Get the thinking tokens and max tokens for the model.
        """
        if thinking_level == ThinkingLevel.LOW:
            return 8_192, {"type": "enabled", "budget_tokens": 4_096}
        elif thinking_level == ThinkingLevel.MEDIUM:
            return 32_768, {"type": "enabled", "budget_tokens": 25_600}
        elif thinking_level == ThinkingLevel.HIGH:
            return 64_000, {"type": "enabled", "budget_tokens": 55_808}

    def draw_mermaid(self):
        """
        Draw the graph in Mermaid format.

        Returns:
            str: The Mermaid graph
        """
        if isinstance(self.agent, CompiledStateGraph):
            return self.agent.get_graph(xray=True).draw_mermaid_png()
        return self.agent.get_graph().draw_mermaid_png()

    def get_num_tokens_from_messages(self, messages: list[BaseMessage], model_name: str) -> int:
        """
        Get the number of tokens from a list of messages.

        Args:
            messages (list[BaseMessage]): The messages
            model_name (str): The model name

        Returns:
            int: The number of tokens
        """
        return self.get_model(model=model_name).get_num_tokens_from_messages(messages)

    def get_max_token_value(self, model_name: str) -> int:
        """
        Get the maximum token value for the model.

        Args:
            model_name (str): The model name

        Returns:
            int: The maximum token value
        """

        match BaseAgent.get_model_provider(model_name):
            case ModelProvider.ANTHROPIC:
                return 8192

            case ModelProvider.OPENAI:
                _, encoding_model = cast("ChatOpenAI", self.get_model(model=model_name))._get_encoding_model()
                return encoding_model.max_token_value

            case ModelProvider.GOOGLE_GENAI:
                # As stated in docs: https://ai.google.dev/gemini-api/docs/models/gemini#gemini-2.0-flash
                return 8192

            case _:
                raise ValueError(f"Unknown provider for model {model_name}")

    @staticmethod
    def get_model_provider(model_name: str) -> ModelProvider:
        """
        Get the model provider.

        Args:
            model_name (str): The model name

        Returns:
            ModelProvider: The model provider
        """
        if any(model_name.startswith(pre) for pre in ("gpt-4", "o1", "o3", "o4")):
            return ModelProvider.OPENAI
        elif model_name.startswith("claude"):
            return ModelProvider.ANTHROPIC
        elif model_name.startswith("gemini"):
            return ModelProvider.GOOGLE_GENAI
        elif model_name.startswith("openrouter:"):
            return ModelProvider.OPENROUTER
        else:
            raise ValueError(f"Unknown/Unsupported provider for model {model_name}")
