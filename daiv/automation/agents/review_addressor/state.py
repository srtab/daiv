from langgraph.graph import MessagesState

from automation.agents.state import PlanExecuteState


class OverallState(PlanExecuteState, MessagesState):
    diff: str
    show_diff_hunk_to_executor: bool
    request_for_changes: bool
