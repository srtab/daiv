from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import AskForClarification, DetermineNextAction, Plan

logger = logging.getLogger("daiv.tools")


@tool("determine_next_action", args_schema=DetermineNextAction)
def determine_next_action(
    action: Plan | AskForClarification, tool_call_id: str
) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Use this tool to determine the next action to take, i.e. when you have a plan to share or when you need to ask for clarification.
    """  # noqa: E501
    logger.info("[determine_next_action] Determining next action: %s", repr(action))

    message = ToolMessage(content=[{"action": action.model_dump()}], tool_call_id=tool_call_id)

    if isinstance(action, AskForClarification):
        return Command(
            goto=END, update={"plan_questions": action.questions, "messages": [message]}, graph=Command.PARENT
        )
    return Command(
        goto="plan_approval",
        update={"plan_tasks": action.changes, "plan_goal": action.goal, "messages": [message]},
        graph=Command.PARENT,
    )


# https://www.anthropic.com/engineering/claude-think-tool


@tool("think", parse_docstring=True)
def think_plan(thought: str):
    """
    Use the tool to think about the plan and the changes to apply to the codebase to address the user request. It will not obtain new information or make any changes, but just log the thought. Use it when complex reasoning or brainstorming is needed. Use it as a scratchpad.

    Args:
        thought: Your thoughts.

    Returns:
        A message indicating that the thought has been logged.
    """  # noqa: E501
    logger.info("[think] Thinking about: %s", thought)
    return "Thought registered."


@tool("think", parse_docstring=True)
def think_plan_executer(thought: str):
    """
    Use the tool to think about implementation approaches to apply code changes to the codebase or to plan the next steps. It will not obtain new information or make any changes, but just log the thought. Use it when complex reasoning or brainstorming is needed. Use it as a scratchpad.

    Args:
        thought: Your thoughts.

    Returns:
        A message indicating that the thought has been logged.
    """  # noqa: E501
    logger.info("[think] Thinking about: %s", thought)
    return "Thought registered."
