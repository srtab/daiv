from abc import ABC, abstractmethod

from langchain.chat_models import init_chat_model
from langchain_community.callbacks import OpenAICallbackHandler
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph

PERFORMANT_GENERIC_MODEL_NAME = "gpt-4o-2024-08-06"
COST_EFFICIENT_GENERIC_MODEL_NAME = "gpt-4o-mini-2024-07-18"
PERFORMANT_CODING_MODEL_NAME = "claude-3-5-sonnet-20240620"


class BaseAgent(ABC):
    """
    Base agent class for creating agents that interact with a model.
    """

    model_name: str = COST_EFFICIENT_GENERIC_MODEL_NAME

    def __init__(self, *, model_name: str | None = None, usage_handler: OpenAICallbackHandler | None = None):
        self.model_name = model_name or self.model_name
        self.usage_handler = usage_handler or OpenAICallbackHandler()
        self.model = self.get_model()
        self.agent = self.compile().with_config(self.get_config())

    @abstractmethod
    def compile(self) -> CompiledStateGraph | Runnable:
        pass

    def get_model(self) -> ChatOpenAI | Runnable[LanguageModelInput, BaseMessage]:
        """
        Get the model instance to use for the agent.

        Returns:
            ChatOpenAI: The model instance
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
            "configurable_fields": ("model", "model_provider", "temperature"),
        }

    def get_config(self) -> RunnableConfig:
        """
        Get the configuration for the agent.

        Returns:
            dict: The configuration
        """
        return RunnableConfig(run_name=self.__class__.__name__, tags=[self.__class__.__name__], metadata={})

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
