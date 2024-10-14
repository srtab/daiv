from langgraph.graph import MessagesState


class PlanExecuteState(MessagesState):
    plan_tasks: list[str]
    goal: str
    response: str
