from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnablePassthrough

from automation.graphs.agents import BaseAgent

from .prompts import human, system
from .schemas import PullRequestDescriberOutput


class PullRequestDescriberAgent(BaseAgent[Runnable]):
    """
    Agent to describe changes in a pull request.
    """

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(system),
            HumanMessagePromptTemplate.from_template(human, "jinja2"),
        ])
        return (
            {"changes": RunnablePassthrough()}
            | prompt
            | self.model.with_structured_output(PullRequestDescriberOutput, method="json_schema")
        )
