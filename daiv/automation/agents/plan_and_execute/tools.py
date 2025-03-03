from typing import Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import AskForClarification, DetermineNextAction, Plan


@tool("determine_next_action", args_schema=DetermineNextAction)
def determine_next_action(
    action: Plan | AskForClarification, tool_call_id: str
) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Determine the next action to take. Choose the appropriate action based on the feedback.
    """
    message = ToolMessage(content={"action": action.model_dump()}, tool_call_id=tool_call_id)

    if isinstance(action, AskForClarification):
        return Command(
            goto=END, update={"plan_questions": action.questions, "messages": [message]}, graph=Command.PARENT
        )
    return Command(
        goto="plan_approval",
        update={"plan_tasks": action.tasks, "plan_goal": action.goal, "messages": [message]},
        graph=Command.PARENT,
    )
