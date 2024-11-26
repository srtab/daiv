from typing import Literal

from automation.agents.pipeline_fixer.schemas import ActionPlan
from automation.agents.state import PlanExecuteState


class OverallState(PlanExecuteState):
    job_logs: str
    diff: str
    category: Literal["codebase", "external-factor"]
    pipeline_phase: Literal["lint", "unittest", "other"]
    root_cause: str
    actions: list[ActionPlan]
