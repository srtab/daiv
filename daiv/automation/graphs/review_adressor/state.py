import operator
from typing import Annotated

from langgraph.graph import MessagesState

from codebase.base import FileChange


class PlanExecute(MessagesState):
    diff: str
    plan_tasks: list[str]
    goal: str
    past_steps: Annotated[list[tuple], operator.add]
    file_changes: dict[str, FileChange]
    response: str
    finished: bool
