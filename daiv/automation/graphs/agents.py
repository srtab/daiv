from abc import ABC, abstractmethod

from langchain.chat_models import init_chat_model
from langchain.chat_models.base import BaseChatModel
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph

GENERIC_PERFORMANT_MODEL_NAME = "gpt-4o-2024-08-06"
CODING_PERFORMANT_MODEL_NAME = "claude-3-5-sonnet-20240620"
PLANING_PERFORMANT_MODEL_NAME = "claude-3-opus-20240229"
GENERIC_COST_EFFICIENT_MODEL_NAME = "gpt-4o-mini-2024-07-18"


class BaseAgent(ABC):
    """
    Base agent class for creating agents that interact with a model.
    """

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
    def compile(self) -> CompiledStateGraph | Runnable:
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
        return {
            "model": self.model_name,
            "temperature": 0,
            "callbacks": [self.usage_handler],
            "configurable_fields": ("model", "model_provider", "temperature", "max_tokens"),
        }

    def get_config(self) -> RunnableConfig:
        """
        Get the configuration for the agent.

        Returns:
            dict: The configuration
        """
        return RunnableConfig(run_name=self.run_name, tags=[self.run_name], metadata={})

    def get_max_token_value(self) -> int:
        """
        Get the maximum token value for the model.
        """
        _, encoding_model = self.model._get_encoding_model()
        return encoding_model.max_token_value

    def draw_mermaid(self):
        """
        Draw the graph in Mermaid format.

        Returns:
            str: The Mermaid graph
        """
        return self.agent.get_graph().draw_mermaid()
