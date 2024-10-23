from automation.agents.state import PlanExecuteState


class OverallState(PlanExecuteState):
    diff: str
    show_diff_hunk_to_executor: bool
    request_for_changes: bool
