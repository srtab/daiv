from typing import TypedDict

from automation.agents.schemas import Task


class PlanExecuteState(TypedDict):
    goal: str
    plan_tasks: list[Task]
    response: str
