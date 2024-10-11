from langgraph.graph import MessagesState

from codebase.base import FileChange


class OverallState(MessagesState):
    diff: str
    plan_tasks: list[str]
    goal: str
    show_diff_hunk_to_executor: bool
    response: str
    request_for_changes: bool
    file_changes: dict[str, FileChange]
