from automation.graphs.state import PlanExecuteState
from codebase.base import FileChange


class OverallState(PlanExecuteState):
    issue_title: str
    issue_description: str
    request_for_changes: bool
    human_approved: bool
    file_changes: dict[str, FileChange]
