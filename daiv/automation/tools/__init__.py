import logging

from langchain_core.tools import tool

logger = logging.getLogger("daiv.tools")

# https://www.anthropic.com/engineering/claude-think-tool


@tool(parse_docstring=True)
def think(thought: str):
    """
    Use the tool to think about something. It will not obtain new information or make any changes, but just log the thought. Use it when complex reasoning or brainstorming is needed. Use it as a scratchpad.

    Args:
        thought: Your thoughts.

    Returns:
        A message indicating that the thought has been logged.
    """  # noqa: E501
    logger.info("[think] Thinking about: %s", thought)
    return "Thought registered."
