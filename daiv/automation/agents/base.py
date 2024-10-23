from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING, Generic, TypeVar

from langchain.chat_models.base import BaseChatModel, _attempt_infer_model_provider, init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from pydantic import BaseModel

if TYPE_CHECKING:
    from langgraph.checkpoint.postgres import PostgresSaver

ANTHROPIC_PROVIDER_NAME = "anthropic"

PLANING_PERFORMANT_MODEL_NAME = "claude-3-opus-20240229"
PLANING_COST_EFFICIENT_MODEL_NAME = "gpt-4o-2024-08-06"
CODING_PERFORMANT_MODEL_NAME = "claude-3-5-sonnet-20241022"
CODING_COST_EFFICIENT_MODEL_NAME = "claude-3-5-sonnet-20241022"  # TODO: Replace with haiku 3.5 when released
GENERIC_PERFORMANT_MODEL_NAME = "gpt-4o-2024-08-06"
GENERIC_COST_EFFICIENT_MODEL_NAME = "gpt-4o-mini-2024-07-18"


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

    def get_model(self) -> BaseChatModel:
        """
        Get the model instance to use for the agent.

        Returns:
            BaseChatModel: The model instance
        """
        return init_chat_model(**self.get_model_kwargs())

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

        if _attempt_infer_model_provider(self.model_name) == ANTHROPIC_PROVIDER_NAME:
            kwargs["model_kwargs"]["extra_headers"] = {
                "anthropic-beta": "prompt-caching-2024-07-31,max-tokens-3-5-sonnet-2024-07-15"
            }
            kwargs["max_tokens"] = "8192" if self.model_name.startswith("claude-3-5-sonnet") else "4096"
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
