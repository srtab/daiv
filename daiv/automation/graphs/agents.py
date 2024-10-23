from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from langchain.chat_models.base import BaseChatModel, _attempt_infer_model_provider, init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver

GENERIC_PERFORMANT_MODEL_NAME = "gpt-4o-2024-08-06"
CODING_PERFORMANT_MODEL_NAME = "claude-3-5-sonnet-20241022"
PLANING_PERFORMANT_MODEL_NAME = "claude-3-opus-20240229"
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
        if _attempt_infer_model_provider(self.model_name) == "anthropic":
            kwargs["model_kwargs"]["extra_headers"] = {
                "anthropic-beta": "prompt-caching-2024-07-31,max-tokens-3-5-sonnet-2024-07-15"
            }
            kwargs["max_tokens"] = "8192"
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
