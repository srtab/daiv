from typing import Literal

from automation.agents.state import PlanExecuteState


class OverallState(PlanExecuteState):
    job_logs: str
    diff: str
    category: Literal["codebase", "external-factor"]
