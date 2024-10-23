from automation.agents.state import PlanExecuteState


class OverallState(PlanExecuteState):
    issue_title: str
    issue_description: str
    request_for_changes: bool
    human_approved: bool
