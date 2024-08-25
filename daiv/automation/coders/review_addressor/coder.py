from typing import Unpack, cast

from automation.agents.agent import LlmAgent
from automation.agents.models import Message
from automation.coders.base import STOP_MESSAGE, CodebaseCoder
from automation.coders.typings import ReviewAddressorInvoke
from codebase.base import FileChange

from .models import RequestFeedback
from .prompts import ReviewAddressorPrompts, ReviewCommentorPrompts
from .tools import ReviewAddressorTools


class ReviewCommentorCoder(CodebaseCoder[ReviewAddressorInvoke, RequestFeedback]):
    """
    Coder to review the comments in the pull request.
    """

    def invoke(self, *args, **kwargs: Unpack[ReviewAddressorInvoke]) -> RequestFeedback:
        """
        Invoke the coder to review the comments in the pull request.
        """
        memory = [Message(role="system", content=ReviewCommentorPrompts.format_system(kwargs["diff"]))]

        for note in kwargs["notes"]:
            # add previous notes to thread the conversation
            if note.author.id == self.repo_client.current_user.id:
                memory.append(Message(role="assistant", content=note.body))
            else:
                memory.append(Message(role="user", content=note.body))

        self.agent = LlmAgent(memory=memory)
        response = self.agent.run(response_model=RequestFeedback, single_iteration=True)

        self.usage += self.agent.usage

        if response is None:
            return []

        return cast(RequestFeedback, response)


class ReviewAddressorCoder(CodebaseCoder[ReviewAddressorInvoke, list[FileChange]]):
    """
    Coder to address the review comments in the codebase.
    """

    def invoke(self, *args, **kwargs: Unpack[ReviewAddressorInvoke]) -> list[FileChange]:
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
            Message(role="system", content=ReviewAddressorPrompts.format_system(kwargs["file_path"], kwargs["diff"]))
        ]

        for note in kwargs["notes"]:
            # add previous notes to thread the conversation
            if note.author.id == self.repo_client.current_user.id:
                memory.append(Message(role="assistant", content=note.body))
            else:
                memory.append(Message(role="user", content=note.body))

        self.agent = LlmAgent(memory=memory, tools=code_actions.get_tools(), stop_messages=[STOP_MESSAGE])
        self.agent.run()

        self.usage += self.agent.usage

        return list(code_actions.file_changes.values())
