from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agent import BaseAgent

from .conf import settings
from .prompts import human, system
from .schemas import PullRequestDescriberInput, PullRequestMetadata

if TYPE_CHECKING:
    from automation.agent.constants import ModelName


class PullRequestDescriberAgent(BaseAgent[Runnable[PullRequestDescriberInput, PullRequestMetadata]]):
    """
    Agent to describe changes in a pull request.
    """

    def __init__(self, *, model: ModelName | str, **kwargs):
        self.model = model
        super().__init__(**kwargs)

    async def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([system, human]).partial(
            current_date_time=timezone.now().strftime("%d %B, %Y"), context_file_content="", extra_context=""
        )
        return (
            prompt | BaseAgent.get_model(model=self.model).with_structured_output(PullRequestMetadata)
        ).with_config({"run_name": settings.NAME})
