from automation.graphs.state import PlanExecuteState
from codebase.base import FileChange, Issue


class OverallState(PlanExecuteState):
    issue: Issue
    request_for_changes: bool
    human_approved: bool
    file_changes: dict[str, FileChange]
