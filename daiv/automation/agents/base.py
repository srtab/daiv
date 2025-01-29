from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar, cast

from langchain.chat_models.base import _attempt_infer_model_provider, init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel

from automation.conf import settings

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_openai.chat_models import ChatOpenAI
    from langgraph.checkpoint.postgres import PostgresSaver


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    GOOGLE_GENAI = "google_genai"


T = TypeVar("T", bound=Runnable)


class BaseAgent[T: Runnable](ABC):
    """
    Base agent class for creating agents that interact with a model.
    """

    agent: T

    model_name: str = settings.GENERIC_COST_EFFICIENT_MODEL_NAME

    def __init__(
        self,
        *,
        run_name: str | None = None,
        model_name: str | None = None,
        usage_handler: OpenAICallbackHandler | None = None,
        checkpointer: PostgresSaver | None = None,
    ):
        self.run_name = run_name or self.__class__.__name__
        self.model_name = model_name or self.model_name
        self.usage_handler = usage_handler or OpenAICallbackHandler()
        self.checkpointer = checkpointer
        self.model = self.get_model()
        self.agent = self.compile().with_config(self.get_config())

    @abstractmethod
    def compile(self) -> T:
        pass

    def get_model(self, **kwargs) -> BaseChatModel:
        """
        Get the model instance to use for the agent.

        Returns:
            BaseChatModel: The model instance
        """
        model_kwargs = self.get_model_kwargs(**kwargs)
        return init_chat_model(**model_kwargs)

    def get_model_kwargs(self, **kwargs) -> dict:
        """
        Get the keyword arguments to pass to the model.

        Returns:
            dict: The keyword arguments
        """
        _kwargs = {
            "model": self.model_name,
            "temperature": 0,
            "callbacks": [self.usage_handler],
            "configurable_fields": ("model", "temperature", "max_tokens"),
            "model_kwargs": {},
            **kwargs,
        }

        model_provider = BaseAgent.get_model_provider(_kwargs["model"])

        if model_provider == ModelProvider.ANTHROPIC:
            # As stated in docs: https://docs.anthropic.com/en/api/rate-limits#updated-rate-limits
            # the OTPM is calculated based on the max_tokens. We need to use a fair value to avoid rate limiting.
            # If needed, we can increase this value using the configurable field.
            _kwargs["max_tokens"] = "2048"
            _kwargs["model_kwargs"]["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}
        elif model_provider == ModelProvider.DEEPSEEK:
            assert settings.DEEPSEEK_API_KEY is not None, "DEEPSEEK_API_KEY is not set"

            _kwargs["model_provider"] = "openai"
            _kwargs["base_url"] = settings.DEEPSEEK_API_BASE
            _kwargs["api_key"] = settings.DEEPSEEK_API_KEY
        elif model_provider == ModelProvider.GOOGLE_GENAI:
            # otherwise it will be inferred as google_vertexai
            _kwargs["model_provider"] = "google_genai"
        return _kwargs

    def get_config(self) -> RunnableConfig:
        """
        Get the configuration for the agent.

        Returns:
            dict: The configuration
        """
        return RunnableConfig(run_name=self.run_name, tags=[self.run_name], metadata={}, configurable={})

    def draw_mermaid(self):
        """
        Draw the graph in Mermaid format.

        Returns:
            str: The Mermaid graph
        """
        return self.agent.get_graph().draw_mermaid()

    def get_num_tokens_from_messages(self, messages: list[BaseMessage]) -> int:
        """
        Get the number of tokens from a list of messages.

        Args:
            messages (list[BaseMessage]): The messages

        Returns:
            int: The number of tokens
        """
        return self.model.get_num_tokens_from_messages(messages)

    def get_max_token_value(self) -> int:
        """
        Get the maximum token value for the model.

        Returns:
            int: The maximum token value
        """

        match BaseAgent.get_model_provider(self.model_name):
            case ModelProvider.ANTHROPIC:
                return 8192

            case ModelProvider.OPENAI:
                _, encoding_model = cast("ChatOpenAI", self.model)._get_encoding_model()
                return encoding_model.max_token_value

            case ModelProvider.DEEPSEEK:
                # As stated in docs: https://api-docs.deepseek.com/quick_start/pricing
                return 8192

            case ModelProvider.GOOGLE_GENAI:
                # As stated in docs: https://ai.google.dev/gemini-api/docs/models/gemini#gemini-2.0-flash
                return 8192

            case _:
                raise ValueError(f"Unknown provider for model {self.model_name}")

    @staticmethod
    def get_model_provider(model_name: str) -> ModelProvider:
        """
        Get the model provider.

        Args:
            model_name (str): The model name

        Returns:
            ModelProvider: The model provider
        """
        if model_name.startswith("deepseek"):
            model_provider = ModelProvider.DEEPSEEK
        elif model_name.startswith("gemini"):
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
