from typing import NotRequired, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent
from automation.agents.base import CODING_COST_EFFICIENT_MODEL_NAME, GENERIC_COST_EFFICIENT_MODEL_NAME
from codebase.base import FileChange

from .prompts import human, system
from .schemas import PullRequestDescriberOutput


class PullRequestDescriberInput(TypedDict):
    changes: list[FileChange]
    extra_details: NotRequired[dict[str, str]]
    branch_name_convention: NotRequired[str]


class PullRequestDescriberAgent(BaseAgent[Runnable[PullRequestDescriberInput, PullRequestDescriberOutput]]):
    """
    Agent to describe changes in a pull request.
    """

    model_name = GENERIC_COST_EFFICIENT_MODEL_NAME

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([system, human]).partial(
            branch_name_convention=None, extra_details={}
        )
        return prompt | self.model.with_structured_output(
            PullRequestDescriberOutput, method="json_schema"
        ).with_fallbacks([
            self.get_model(model=CODING_COST_EFFICIENT_MODEL_NAME).with_structured_output(PullRequestDescriberOutput)
        ])
