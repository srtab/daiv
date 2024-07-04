from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.typings import RefactorInvoke
from codebase.base import FileChange

from .base import RefactorCoder
from .prompts import RefactorPrompts
from .tools import CodeActionTools


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
            Message(role="user", content=kwargs["prompt"]),
        ]

        code_actions = CodeActionTools(
            self.repo_client,
            self.codebase_index,
            self.usage,
            repo_id=kwargs["source_repo_id"],
            ref=kwargs["source_ref"],
        )

        self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_message="<DONE>")

        response = self.agent.run()

        self.usage += self.agent.usage

        if response is None:
            return []

        return list(code_actions.file_changes.values())
