import logging
from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.typings import RefactorInvoke
from codebase.models import FileChange

from .base import RefactorCoder
from .prompts import RefactorPrompts
from .tools import CodeActionTools

logger = logging.getLogger(__name__)


class SimpleRefactorCoder(RefactorCoder[RefactorInvoke, list[FileChange]]):
    """
    A simple coder that takes the user input and generates the original and updated blocks.
    """

    def invoke(self, *args, **kwargs: Unpack[RefactorInvoke]) -> list[FileChange]:
        """
        Invoke the coder to generate the original and updated blocks from the user input.
        """
        memory = [
            Message(role="system", content=RefactorPrompts.format_system()),
            Message(
                role="user",
                content=RefactorPrompts.format_files_to_change(self.get_repo_files_prompt(kwargs["files_to_change"])),
            ),
            Message(role="assistant", content="Ok."),
            Message(role="user", content=RefactorPrompts.format_user_prompt(kwargs["prompt"])),
        ]

        if kwargs["changes_example_file"] is not None:
            memory.append(
                Message(
                    role="user",
                    content=RefactorPrompts.format_refactor_example(
                        self.get_repo_files_prompt([kwargs["changes_example_file"]])
                    ),
                )
            )

        code_actions = CodeActionTools(
            self.repo_client, kwargs["files_to_change"][0].repo_id, kwargs["files_to_change"][0].ref
        )
        self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")

        if self.agent.run() is None:
            return []

        return list(code_actions.file_changes.values())
