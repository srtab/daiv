from functools import cached_property

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from automation.agents import CODING_COST_EFFICIENT_MODEL_NAME, BaseAgent

from .prompts import human, system
from .schemas import SnippetReplacerOutput


class SnippetReplacerInput(BaseModel):
    """
    Input for the SnippetReplacerAgent.
    """

    original_snippet: str = ""
    replacement_snippet: str = ""
    content: str = ""


class SnippetReplacerAgent(BaseAgent[Runnable]):
    """
    Agent to replace a code snippet in a codebase.
    """

    model_name = CODING_COST_EFFICIENT_MODEL_NAME

    def compile(self) -> Runnable:
        """
        Compile the agent.

        Returns:
            CompiledStateGraph | Runnable: The compiled agent.
        """
        return self._prompt | self.model.with_structured_output(SnippetReplacerOutput, method="json_schema")

    @cached_property
    def _prompt(self) -> ChatPromptTemplate:
        """
        Get the prompt for the agent.

        Returns:
            ChatPromptTemplate: The prompt.
        """
        return ChatPromptTemplate.from_messages([
            # cache-control: ephemeral can only be used with anthropic models
            SystemMessage(system, additional_kwargs={"cache-control": {"type": "ephemeral"}}),
            HumanMessagePromptTemplate.from_template(human),
        ])
