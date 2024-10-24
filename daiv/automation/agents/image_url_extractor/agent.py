from typing import TypedDict, cast

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig

from automation.agents import BaseAgent
from automation.agents.image_url_extractor.prompts import human, system
from automation.agents.image_url_extractor.schemas import ImageTemplate, ImageURLExtractorOutput


class AgentInput(TypedDict):
    markdown_text: str


def _post_process(output: ImageURLExtractorOutput, config: RunnableConfig) -> list[dict]:
    """
    Post-process the extracted images.

    Args:
        output (ImageURLExtractorOutput): The extracted images.
        config (RunnableConfig): The configuration for the agent.

    Returns:
        list[dict]: The processed images ready to be used on prompt templates.
    """
    if not (project_id := config["configurable"].get("project_id")):
        raise ValueError("Project ID is required to extract image URLs.")

    return ImageTemplate.from_images(cast(int, project_id), output.images)


class ImageURLExtractorAgent(BaseAgent[Runnable[AgentInput, list[dict]]]):
    """
    Agent to extract image URLs from a markdown text.
    """

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(system),
            HumanMessagePromptTemplate.from_template(human, "jinja2"),
        ])
        return prompt | self.model.with_structured_output(ImageURLExtractorOutput, method="json_schema") | _post_process
