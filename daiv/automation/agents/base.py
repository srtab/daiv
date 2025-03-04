from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from langchain.chat_models.base import _attempt_infer_model_provider, init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_openai.chat_models import ChatOpenAI
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.store.base import BaseStore


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    GOOGLE_GENAI = "google_genai"


class ThinkingLevel(StrEnum):
    SOFT = "soft"
    MEDIUM = "medium"
    HARD = "hard"


T = TypeVar("T", bound=Runnable)


class BaseAgent(ABC, Generic[T]):  # noqa: UP046
    """
    Base agent class for creating agents that interact with a model.
    """

    agent: T

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
        self.agent = self.compile()

    @abstractmethod
    def compile(self) -> T:
        pass

    def get_model(self, *, model: str, thinking_level: ThinkingLevel | None = None, **kwargs) -> BaseChatModel:
        """
        Get the model instance to use for the agent.

        Returns:
            BaseChatModel: The model instance
        """
        model_kwargs = self.get_model_kwargs(model=model, thinking_level=thinking_level, **kwargs)
        return init_chat_model(**model_kwargs)

    def get_model_kwargs(self, *, thinking_level: ThinkingLevel | None = None, **kwargs) -> dict:
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
            **kwargs,
        }

        model_provider = BaseAgent.get_model_provider(_kwargs["model"])

        if model_provider == ModelProvider.ANTHROPIC:
            # As stated in docs: https://docs.anthropic.com/en/api/rate-limits#updated-rate-limits
            # the OTPM is calculated based on the max_tokens. We need to use a fair value to avoid rate limiting.
            # If needed, we can increase this value using the configurable field.
            if thinking_level and _kwargs["model"].startswith("claude-3-7-sonnet"):
                # When using thinking the temperature need to be set to 1
                _kwargs["temperature"] = 1
                if thinking_level == ThinkingLevel.SOFT:
                    _kwargs["max_tokens"] = 4_000
                    _kwargs["thinking"] = {"type": "enabled", "budget_tokens": 2_000}
                elif thinking_level == ThinkingLevel.MEDIUM:
                    _kwargs["max_tokens"] = 10_000
                    _kwargs["thinking"] = {"type": "enabled", "budget_tokens": 7_000}
                elif thinking_level == ThinkingLevel.HARD:
                    _kwargs["max_tokens"] = 20_000
                    _kwargs["thinking"] = {"type": "enabled", "budget_tokens": 16_000}
            else:
                _kwargs["max_tokens"] = 2048
        elif model_provider in [ModelProvider.GOOGLE_GENAI]:
            # otherwise google_genai will be inferred as google_vertexai
            _kwargs["model_provider"] = model_provider
        return _kwargs

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

            case ModelProvider.DEEPSEEK:
                # As stated in docs: https://api-docs.deepseek.com/quick_start/pricing
                return 8192

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
        model_provider: ModelProvider | None = None

        if model_name.startswith("gemini"):
            model_provider = ModelProvider.GOOGLE_GENAI
        else:
            model_provider = cast("ModelProvider | None", _attempt_infer_model_provider(model_name))

        if model_provider is None:
            raise ValueError(f"Unknown provider for model {model_name}")

        return model_provider


class Usage(BaseModel):
    completion_tokens: int = 0
    """The number of tokens used for completion."""

    prompt_tokens: int = 0
    """The number of tokens used for the prompt."""

    total_tokens: int = 0
    """The total number of tokens used."""

    prompt_cost: Decimal = Decimal(0.0)
    """The cost of the prompt tokens."""

    completion_cost: Decimal = Decimal(0.0)
    """The cost of the completion tokens."""

    total_cost: Decimal = Decimal(0.0)
    """The total cost of the tokens."""

    def __add__(self, other: Usage):
        self.completion_tokens += other.completion_tokens
        self.prompt_tokens += other.prompt_tokens
        self.total_tokens += other.total_tokens
        self.prompt_cost += other.prompt_cost
        self.completion_cost += other.completion_cost
        self.total_cost += other.total_cost
        return self
