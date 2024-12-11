from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from langchain.chat_models.base import BaseChatModel, _attempt_infer_model_provider, init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai.chat_models import ChatOpenAI
from pydantic import BaseModel

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage
    from langgraph.checkpoint.postgres import PostgresSaver

PLANING_PERFORMANT_MODEL_NAME = "claude-3-5-sonnet-20241022"
PLANING_COST_EFFICIENT_MODEL_NAME = "claude-3-5-sonnet-20241022"
CODING_PERFORMANT_MODEL_NAME = "claude-3-5-sonnet-20241022"
CODING_COST_EFFICIENT_MODEL_NAME = "claude-3-5-haiku-20241022"
GENERIC_PERFORMANT_MODEL_NAME = "gpt-4o-2024-11-20"
GENERIC_COST_EFFICIENT_MODEL_NAME = "gpt-4o-mini-2024-07-18"


class ModelProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


T = TypeVar("T", bound=Runnable)


class BaseAgent(ABC, Generic[T]):
    """
    Base agent class for creating agents that interact with a model.
    """

    agent: T

    model_name: str = GENERIC_COST_EFFICIENT_MODEL_NAME

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
        model_kwargs = self.get_model_kwargs()
        model_kwargs.update(kwargs)
        return init_chat_model(**model_kwargs)

    def get_model_kwargs(self) -> dict:
        """
        Get the keyword arguments to pass to the model.

        Returns:
            dict: The keyword arguments
        """
        kwargs = {
            "model": self.model_name,
            "temperature": 0,
            "callbacks": [self.usage_handler],
            "configurable_fields": ("model", "temperature", "max_tokens"),
            "model_kwargs": {},
        }

        if self.get_model_provider() == ModelProvider.ANTHROPIC:
            kwargs["model_kwargs"]["extra_headers"] = {
                "anthropic-beta": "prompt-caching-2024-07-31,max-tokens-3-5-sonnet-2024-07-15"
            }
            kwargs["max_tokens"] = str(self.get_max_token_value())
        return kwargs

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

        match self.get_model_provider():
            case ModelProvider.ANTHROPIC:
                return 8192 if self.model_name.startswith(("claude-3-5-sonnet", "claude-3-5-haiku")) else 4096

            case ModelProvider.OPENAI:
                _, encoding_model = cast(ChatOpenAI, self.model)._get_encoding_model()
                return encoding_model.max_token_value

            case _:
                raise ValueError(f"Unknown provider for model {self.model_name}")

    def get_model_provider(self) -> ModelProvider:
        """
        Get the model provider.

        Returns:
            ModelProvider: The model provider
        """
        return _attempt_infer_model_provider(self.model_name)


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
