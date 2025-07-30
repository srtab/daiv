from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import AskForClarification, CompleteWithClarification, CompleteWithPlan, Plan

logger = logging.getLogger("daiv.tools")


@tool("complete_with_plan", args_schema=CompleteWithPlan)
def complete_with_plan(plan: Plan, tool_call_id: str) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Use this tool to complete the task, i.e. when you have a plan to share.
    """  # noqa: E501
    logger.info("[complete_with_plan] Determining next action: %s", repr(plan))

    message = ToolMessage(content=[{"plan": plan.model_dump()}], tool_call_id=tool_call_id)

    return Command(
        goto="plan_approval", update={"plan_tasks": plan.changes, "messages": [message]}, graph=Command.PARENT
    )


@tool("complete_with_clarification", args_schema=CompleteWithClarification)
def complete_with_clarification(
    ask_for_clarification: AskForClarification, tool_call_id: str
) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Use this tool to ask for clarification.
    """  # noqa: E501
    logger.info("[complete_with_clarification] Determining next action: %s", repr(ask_for_clarification))

    message = ToolMessage(
        content=[{"ask_for_clarification": ask_for_clarification.model_dump()}], tool_call_id=tool_call_id
    )

    return Command(
        goto=END,
        update={"plan_questions": ask_for_clarification.questions, "messages": [message]},
        graph=Command.PARENT,
    )
