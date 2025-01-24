from typing import TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

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
    return ImageTemplate.from_images(
        output.images,
        repo_client_slug=config["configurable"].get("repo_client_slug"),
        project_id=config["configurable"].get("project_id"),
        only_base64=config["configurable"].get("only_base64", False),
    )


class ImageURLExtractorAgent(BaseAgent[Runnable[AgentInput, list[dict]]]):
    """
    Agent to extract image URLs from a markdown text.
    """

    def compile(self) -> Runnable:
        prompt = ChatPromptTemplate.from_messages([system, human])
        return (
            prompt
            | self.model.with_structured_output(ImageURLExtractorOutput)
            | RunnableLambda(_post_process, name="post_process_extracted_images")
        )
