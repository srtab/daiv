from typing import NotRequired, TypedDict

from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, SystemMessagePromptTemplate
from langchain_core.runnables import Runnable

from automation.agents import BaseAgent
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

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(system, "jinja2"),
            HumanMessagePromptTemplate.from_template(human, "jinja2"),
        ]).partial(branch_name_convention=None, extra_details={})
        return prompt | self.model.with_structured_output(PullRequestDescriberOutput, method="json_schema")
