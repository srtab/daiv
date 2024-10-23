from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable
from langchain_core.runnables.utils import Input

from automation.agents import BaseAgent

from .prompts import human, system
from .schemas import ImageURLExtractorOutput


class ImageURLExtractorAgent(BaseAgent[Runnable[Input, ImageURLExtractorOutput]]):
    """
    Agent to extract image URLs from a markdown text.
    """

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(system),
            HumanMessagePromptTemplate.from_template(human, "jinja2"),
        ])
        return prompt | self.model.with_structured_output(ImageURLExtractorOutput, method="json_schema")
