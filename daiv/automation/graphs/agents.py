from abc import ABC, abstractmethod

from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph


class BaseAgent(ABC):
    """
    Base agent class for creating agents that interact with a model.
    """

    model_class: type[ChatOpenAI] = ChatOpenAI
    model_name: str = "gpt-4o-mini-2024-07-18"

    def __init__(self):
        self.model = self.get_model()
        self.graph = self.compile()

    @abstractmethod
    def compile(self) -> CompiledStateGraph:
        pass

    def get_model(self) -> ChatOpenAI:
        """
        Get the model instance to use for the agent.

        Returns:
            ChatOpenAI: The model instance
        """
        return self.model_class(**self.get_model_kwargs())

    def get_model_kwargs(self) -> dict:
        """
        Get the keyword arguments to pass to the model.

        Returns:
            dict: The keyword arguments
        """
        return {"model": self.model_name, "temperature": 0}

    def draw_mermaid(self):
        """
        Draw the graph in Mermaid format.

        Returns:
            str: The Mermaid graph
        """
        return self.graph.get_graph().draw_mermaid()
