from typing import TYPE_CHECKING, Literal

from langgraph.prebuilt.chat_agent_executor import AgentState
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from .schemas import TroubleshootingDetail


class InputState(TypedDict):
    diff: str
    """
    The diff of the changes made to the codebase.
    """

    job_logs: str
    """
    The logs of the job that failed.
    """

    format_iteration: int
    """
    The number of times the format code has been applied.
    """


class OutputState(TypedDict):
    troubleshooting: list[TroubleshootingDetail]
    """
    The troubleshooting details of the job that failed.
    """

    need_manual_fix: bool
    """
    Whether the agent couldnt fix the issue and needs a manual fix.
    """

    pipeline_phase: Literal["lint", "unittest", "other"]
    """
    The phase of the pipeline that failed.
    """


class OverallState(InputState, OutputState):
    pass


class TroubleshootState(AgentState):
    diff: str
    """
    The diff of the changes made to the codebase.
    """

    job_logs: str
    """
    The logs of the job that failed.
    """
