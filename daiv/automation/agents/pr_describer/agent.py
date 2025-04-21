from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, TypedDict

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent

from .conf import settings
from .prompts import human, system
from .schemas import PullRequestDescriberOutput

if TYPE_CHECKING:
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
        prompt = ChatPromptTemplate.from_messages([system, human]).partial(
            branch_name_convention=None, extra_context="", current_date_time=timezone.now().isoformat()
        )
        return (
            prompt
            | self.get_model(model=settings.MODEL_NAME).with_structured_output(
                PullRequestDescriberOutput, method="function_calling"
            )
        ).with_config({"run_name": settings.NAME})
