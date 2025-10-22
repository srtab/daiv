from __future__ import annotations

import logging

from langchain_core.tools import tool

logger = logging.getLogger("daiv.tools")


PLAN_THINK_TOOL_NAME = "think"


@tool(PLAN_THINK_TOOL_NAME, parse_docstring=True)
def plan_think_tool(plan: str):
    """
    Use this tool to outline what you need to investigate to assist the user. This helps you track progress and organize complex tasks in a structured way.

    **Usage rules:**
    - Does NOT fetch new information or modify anything, it's just a placeholder to help you track progress.
    - Update tasks as you learn new information to help you track progress.
    - **Important:** It is critical that you mark tasks as completed as soon as you are done with them. Do not batch up multiple tasks before marking them as completed.

    **Skip using this tool when:**
    - There is only a single, straightforward task
    - The task is trivial and tracking it provides no organizational benefit
    - The task can be completed in less than 3 trivial steps
    - The task is purely conversational or informational

    Args:
        plan (str): The plan to investigate.

    Returns:
        A message indicating that the thought has been registered.
    """  # noqa: E501
    logger.info("[%s] Thinking about: %s", plan_think_tool.name, plan)
    return "Thought registered."
