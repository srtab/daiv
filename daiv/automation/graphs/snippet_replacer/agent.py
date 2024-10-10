from functools import cached_property

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from automation.graphs.agents import BaseAgent

from .prompts import human, system
from .schemas import SnippetReplacerOutput


class SnippetReplacerInput(BaseModel):
    """
    Input for the SnippetReplacerAgent.
    """

    original_snippet: str = ""
    replacement_snippet: str = ""
    content: str = ""


class SnippetReplacerAgent(BaseAgent):
    """
    Agent to replace a code snippet in a codebase.
    """

    def compile(self) -> CompiledStateGraph | Runnable:
        """
        Compile the agent.

        Returns:
            CompiledStateGraph | Runnable: The compiled agent.
        """
        return self._prompt | self.model.with_structured_output(SnippetReplacerOutput, method="json_schema")

    def validate_max_token_not_exceeded(self, input: dict) -> bool:  # noqa: A002
        """
        Validate that the messages does not exceed the maximum token value of the model.

        Args:
            input (dict): The input for the agent

        Returns:
            bool: True if the text does not exceed the maximum token value, False otherwise
        """
        prompt = self._prompt
        filled_messages = prompt.invoke(input).to_messages()
        empty_messages = prompt.invoke(SnippetReplacerInput().model_dump()).to_messages()
        # get the number of tokens used in the messages
        used_tokens = self.model.get_num_tokens_from_messages(filled_messages)
        # try to anticipate the number of tokens needed for the output
        estimated_needed_tokens = used_tokens - self.model.get_num_tokens_from_messages(empty_messages)
        return estimated_needed_tokens <= self.get_max_token_value() - used_tokens

    @cached_property
    def _prompt(self) -> ChatPromptTemplate:
        """
        Get the prompt for the agent.

        Returns:
            ChatPromptTemplate: The prompt.
        """
        return ChatPromptTemplate.from_messages([
            SystemMessage(system),
            HumanMessagePromptTemplate.from_template(human),
        ])
