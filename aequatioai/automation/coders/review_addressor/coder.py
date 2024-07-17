from typing import Unpack

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import STOP_MESSAGE, CodebaseCoder
from automation.coders.typings import ReviewAddressorInvoke
from codebase.base import FileChange

from .prompts import ReviewAddressorPrompts
from .tools import ReviewAddressorTools

STOP_MESSAGE_QUESTION = "<DONE_WITH_QUESTION>"


class ReviewAddressorCoder(CodebaseCoder[ReviewAddressorInvoke, tuple[list[FileChange], list[str]]]):
    """
    Coder to address the review comments in the codebase.
    """

    def invoke(self, *args, **kwargs: Unpack[ReviewAddressorInvoke]) -> tuple[list[FileChange], list[str]]:
        """
        Invoke the coder to address the review comments in the codebase.
        """
        code_actions = ReviewAddressorTools(
            self.repo_client,
            self.codebase_index,
            self.usage,
            repo_id=kwargs["source_repo_id"],
            ref=kwargs["source_ref"],
        )

        memory = [
            Message(role="system", content=ReviewAddressorPrompts.format_system()),
            Message(role="user", content=ReviewAddressorPrompts.format_review_task_prompt(kwargs["file_path"])),
        ]

        if kwargs["hunk"]:
            memory.append(Message(role="user", content=ReviewAddressorPrompts.format_review_hunk_prompt()))
            memory.append(Message(role="user", content=kwargs["hunk"]))

        memory.append(
            Message(role="assistant", content="Could you please pass on the comments you have left for me to address?")
        )

        for note in kwargs["notes"]:
            # add previous notes to thread the conversation
            if note.author.id == self.repo_client.current_user.id:
                memory.append(Message(role="assistant", content=note.body))
            else:
                memory.append(Message(role="user", content=note.body))

        self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_messages=[STOP_MESSAGE])
        self.agent.run()

        self.usage += self.agent.usage

        if code_actions.questions:
            return [], code_actions.questions

        return list(code_actions.file_changes.values()), []
