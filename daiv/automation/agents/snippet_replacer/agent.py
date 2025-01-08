from functools import cached_property
from typing import TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda

from automation.agents import BaseAgent

from .conf import settings
from .prompts import human, system
from .schemas import SnippetReplacerOutput
from .utils import find_original_snippet


class SnippetReplacerInput(TypedDict):
    """
    Input for the SnippetReplacerAgent.
    """

    original_snippet: str
    replacement_snippet: str
    content: str


class SnippetReplacerAgent(BaseAgent[Runnable[SnippetReplacerInput, SnippetReplacerOutput | str]]):
    """
    Agent to replace a code snippet in a codebase.
    """

    model_name = settings.MODEL

    def compile(self) -> Runnable:
        """
        Compile the agent.

        Returns:
            CompiledStateGraph | Runnable: The compiled agent.
        """
        return (
            RunnableLambda(self._route, name="route_replacer")
            | RunnableLambda(self._post_process, name="post_process_output")
        ).with_fallbacks([RunnableLambda(self._replace_content_snippet)])

    def _route(self, input_data: SnippetReplacerInput) -> Runnable:
        """
        Route the input data to the appropriate method.

        Args:
            input_data (SnippetReplacerInput): The input data

        Returns:
            Runnable: The appropriate method
        """
        if settings.STRATEGY == "llm" and self.validate_max_token_not_exceeded(input_data):
            return self._prompt | self.model.with_structured_output(SnippetReplacerOutput, method="json_schema")
        return RunnableLambda(self._replace_content_snippet)

    def _replace_content_snippet(self, input_data: SnippetReplacerInput) -> SnippetReplacerOutput | str:
        """
        Replace the content snippet in the content.

        Args:
            input_data (SnippetReplacerInput): The input data

        Returns:
            SnippetReplacerOutput | str: The output
        """
        original_snippet_found = find_original_snippet(
            input_data["original_snippet"], input_data["content"], initial_line_threshold=1
        )
        if not original_snippet_found:
            return "error: Original snippet not found."

        if len(original_snippet_found) > 1:
            return "error: Multiple original snippets found. Please provide a more specific original snippet."

        replaced_content = input_data["content"].replace(original_snippet_found[0], input_data["replacement_snippet"])
        if not replaced_content:
            return "error: Snippet replacement failed."

        return SnippetReplacerOutput(content=replaced_content)

    def _post_process(self, output: SnippetReplacerOutput | str) -> SnippetReplacerOutput | str:
        """
        Post-process the output to ensure it ends with a newline character.

        Args:
            output (SnippetReplacerOutput | str): The output to post-process

        Returns:
            SnippetReplacerOutput | str: The post-processed output
        """
        if isinstance(output, SnippetReplacerOutput) and not output.content.endswith("\n"):
            output.content += "\n"
        return output

    def validate_max_token_not_exceeded(self, input_data: SnippetReplacerInput) -> bool:  # noqa: A002
        """
        Validate that the messages does not exceed the maximum token value of the model.

        Args:
            input (dict): The input for the agent

        Returns:
            bool: True if the text does not exceed the maximum token value, False otherwise
        """
        prompt = self._prompt
        filled_messages = prompt.invoke(input_data).to_messages()
        empty_messages = prompt.invoke({"original_snippet": "", "replacement_snippet": "", "content": ""}).to_messages()
        # try to anticipate the number of tokens needed for the output
        estimated_needed_tokens = self.get_num_tokens_from_messages(
            filled_messages
        ) - self.get_num_tokens_from_messages(empty_messages)
        return estimated_needed_tokens <= self.get_max_token_value()

    @cached_property
    def _prompt(self) -> ChatPromptTemplate:
        """
        Get the prompt for the agent.

        Returns:
            ChatPromptTemplate: The prompt.
        """
        return ChatPromptTemplate.from_messages([system, human])

    def get_max_token_value(self) -> int:
        """
        Get the maximum token value for the model to increase the chances of modal usage on replacement.

        Returns:
            int: The maximum token value
        """
        return 8192
