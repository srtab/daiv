from langgraph.graph import MessagesState

from codebase.base import FileChange


class PlanExecute(MessagesState):
    diff: str
    plan_tasks: list[str]
    goal: str
    response: str
    finished: bool
    file_changes: dict[str, FileChange]
