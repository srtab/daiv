from typing_extensions import TypedDict

from .schemas import TroubleshootingDetail


class OverallState(TypedDict):
    diff: str
    """
    The diff of the changes made to the codebase.
    """

    job_logs: str
    """
    The logs of the job that failed.
    """

    previous_job_logs: str | None
    """
    The logs of the previous job that failed.
    """

    troubleshooting: list[TroubleshootingDetail]
    """
    The troubleshooting details of the job that failed.
    """

    iteration: int
    """
    The iteration of the agent.
    """

    need_manual_fix: bool
    """
    Whether the agent couldnt fix the issue and needs a manual fix.
    """
