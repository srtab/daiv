from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import (
    COMPLETE_WITH_CLARIFICATION_DESCRIPTION,
    COMPLETE_WITH_PLAN_DESCRIPTION,
    AskForClarification,
    ChangeInstructions,
    Plan,
)

logger = logging.getLogger("daiv.tools")


@tool("complete_with_plan", args_schema=Plan, description=COMPLETE_WITH_PLAN_DESCRIPTION)
def complete_with_plan(changes: list[ChangeInstructions], tool_call_id: str) -> Command[Literal["plan_approval"]]:
    """
    Use this tool to complete the task, i.e. when you have a plan to share.

    Args:
        changes (list[ChangeInstructions]): The plan to execute.

    Returns:
        Command[Literal["plan_approval"]]: The next step in the workflow.
    """  # noqa: E501
    logger.info("[complete_with_plan] The plan to execute: %s", repr(changes))

    message = ToolMessage(content=[{"changes": changes}], tool_call_id=tool_call_id)

    return Command(goto="plan_approval", update={"plan_tasks": changes, "messages": [message]}, graph=Command.PARENT)


@tool(
    "complete_with_clarification", args_schema=AskForClarification, description=COMPLETE_WITH_CLARIFICATION_DESCRIPTION
)
def complete_with_clarification(questions: str, tool_call_id: str) -> Command[Literal["__end__"]]:
    """
    Use this tool to ask for clarification.

    Args:
        questions (str): The question(s) to ask the user for clarification.

    Returns:
        Command[Literal["__end__"]]: The next step in the workflow.
    """  # noqa: E501
    logger.info("[complete_with_clarification] The question(s) to ask the user for clarification: %s", repr(questions))

    message = ToolMessage(content=[{"questions": questions}], tool_call_id=tool_call_id)

    return Command(goto=END, update={"plan_questions": questions, "messages": [message]}, graph=Command.PARENT)
