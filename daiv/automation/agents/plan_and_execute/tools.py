from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import AskForClarification, DetermineNextAction, Plan

logger = logging.getLogger("daiv.tools")


@tool("complete_task", args_schema=DetermineNextAction)
def complete_task(
    action: Plan | AskForClarification, tool_call_id: str
) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Use this tool to determine the next action to take, i.e. when you have a plan to share or when you need to ask for clarification.
    """  # noqa: E501
    logger.info("[complete_task] Determining next action: %s", repr(action))

    message = ToolMessage(content=[{"action": action.model_dump()}], tool_call_id=tool_call_id)

    if isinstance(action, AskForClarification):
        return Command(
            goto=END, update={"plan_questions": action.questions, "messages": [message]}, graph=Command.PARENT
        )
    return Command(
        goto="plan_approval", update={"plan_tasks": action.changes, "messages": [message]}, graph=Command.PARENT
    )
