from langgraph.graph import MessagesState

from automation.agents.schemas import Task


class PlanExecuteState(MessagesState):
    plan_tasks: list[Task]
    goal: str
    response: str
