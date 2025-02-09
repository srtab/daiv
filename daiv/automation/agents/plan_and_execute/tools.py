from typing import Literal

from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import AskForClarification, DetermineNextAction, Plan


@tool("determine_next_action", args_schema=DetermineNextAction)
def determine_next_action(action: Plan | AskForClarification) -> Command[Literal["plan_approval", "__end__"]]:
    """
    Determine the next action to take. Choose the appropriate action based on the feedback.
    Communicate in the first person, as if speaking directly to the human.
    Be clear, concise, and professional in your responses, tasks, or questions.
    """
    if isinstance(action, AskForClarification):
        return Command(goto=END, update={"plan_questions": action.questions}, graph=Command.PARENT)
    return Command(
        goto="plan_approval", update={"plan_tasks": action.tasks, "plan_goal": action.goal}, graph=Command.PARENT
    )
