import logging

from langchain_core.tools import tool

logger = logging.getLogger("daiv.tools")

# https://www.anthropic.com/engineering/claude-think-tool

THINK_TOOL_NAME = "think"


@tool(THINK_TOOL_NAME, parse_docstring=True)
def think_tool(thought: str):
    """
    Use the tool to think about something in private. It will not obtain new information or make any changes, but just log the thought. Use it when complex reasoning or brainstorming is needed. Use it as a private scratchpad.

    Args:
        thought (str): Your private thoughts.

    Returns:
        A message indicating that the thought has been logged.
    """  # noqa: E501
    logger.info("[%s] Thinking about: %s", think_tool.name, thought)
    return "Thought registered."
