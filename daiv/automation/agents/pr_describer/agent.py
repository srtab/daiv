from __future__ import annotations

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent

from .conf import settings
from .prompts import system
from .schemas import PullRequestDescriberInput, PullRequestMetadata


class PullRequestDescriberAgent(BaseAgent[Runnable[PullRequestDescriberInput, PullRequestMetadata]]):
    """
    Agent to describe changes in a pull request.
    """

    async def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([system]).partial(
            branch_name_convention=None,
            extra_context="",
            current_date_time=timezone.now().isoformat(timespec="minutes"),
        )
        return (
            prompt
            | self.get_model(model=settings.MODEL_NAME).with_structured_output(
                PullRequestMetadata, method="function_calling"
            )
        ).with_config({"run_name": settings.NAME})
