from typing import Literal

from typing_extensions import TypedDict

from automation.agents.pipeline_fixer.schemas import ActionPlan


class OverallState(TypedDict):
    job_logs: str
    diff: str
    category: Literal["codebase", "external-factor"]
    pipeline_phase: Literal["lint", "unittest", "other"]
    root_cause: str
    actions: list[ActionPlan]
    iteration: int
