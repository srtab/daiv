from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent
from automation.conf import settings

from .prompts import human, system
from .schemas import PullRequestDescriberOutput

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

    from codebase.base import FileChange


class PullRequestDescriberInput(TypedDict):
    changes: list[FileChange]
    extra_context: NotRequired[str]
    branch_name_convention: NotRequired[str]


class PullRequestDescriberAgent(BaseAgent[Runnable[PullRequestDescriberInput, PullRequestDescriberOutput]]):
    """
    Agent to describe changes in a pull request.
    """

    def compile(self) -> Runnable:
        return ChatPromptTemplate.from_messages([system, human]).partial(
            branch_name_convention=None, extra_context=""
        ) | self.get_model(model=settings.PR_DESCRIBER.MODEL_NAME).with_structured_output(
            PullRequestDescriberOutput
        ).with_fallbacks([
            cast(
                "BaseChatModel", self.get_model(model=settings.PR_DESCRIBER.FALLBACK_MODEL_NAME)
            ).with_structured_output(PullRequestDescriberOutput)
        ])
