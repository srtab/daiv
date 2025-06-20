from typing import TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda

from automation.agents import BaseAgent

from .conf import settings
from .prompts import human, system
from .schemas import ImageTemplate, ImageURLExtractorOutput


class AgentInput(TypedDict):
    markdown_text: str


async def _post_process(output: ImageURLExtractorOutput, config: RunnableConfig) -> list[ImageTemplate]:
    """
    Post-process the extracted images.

    Args:
        output (ImageURLExtractorOutput): The extracted images.
        config (RunnableConfig): The configuration for the agent.

    Returns:
        list[ImageTemplate]: The processed images ready to be used on prompt templates.
    """
    return await ImageTemplate.from_images(
        output.images,
        repo_client_slug=config["configurable"].get("repo_client_slug"),
        project_id=config["configurable"].get("project_id"),
    )


class ImageURLExtractorAgent(BaseAgent[Runnable[AgentInput, list[ImageTemplate]]]):
    """
    Agent to extract image URLs from a markdown text.
    """

    async def compile(self) -> Runnable:
        return (
            ChatPromptTemplate.from_messages([system, human])
            | self.get_model(model=settings.MODEL_NAME).with_structured_output(
                ImageURLExtractorOutput, method="function_calling"
            )
            | RunnableLambda(_post_process, name="post_process_extracted_images")
        ).with_config({"run_name": settings.NAME})
