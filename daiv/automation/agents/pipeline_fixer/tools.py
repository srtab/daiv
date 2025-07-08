from typing import Literal

from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

from .schemas import TroubleshootingDetail


@tool("complete_task", parse_docstring=True)
def complete_task(
    category: Literal["codebase", "external-factor"],
    category_reasoning: str,
    pipeline_phase: Literal["lint", "unittest", "other"],
    pipeline_phase_reasoning: str,
    troubleshooting: list[TroubleshootingDetail],
) -> Command[Literal["execute_remediation_steps", "apply_format_code", "__end__"]]:
    """
    Call this tool to complete the task by providing the final categorization and troubleshooting details.

    Args:
        category: State whether the issue is 'codebase' or 'external-factor'.
            If there is any possibility that the issue could be caused by an external factor, classify it as
            'external-factor'.
        category_reasoning: Explain the reasoning behind your categorization, including any external factors
            that may have contributed to the failure.
        pipeline_phase: Identify the phase when the pipeline failed according to the job logs output.
            Use 'unittest' if the issue relates to unit tests, 'lint' for linting problems,
            or 'other' for any other phase.
        pipeline_phase_reasoning: Explain the reasoning behind your pipeline phase categorization.
        troubleshooting: A list of troubleshooting details for each identified error message. If there are no
            troubleshooting details, no error messages or the pipeline succeeded, return an empty list.

    Returns:
        Command[Literal["execute_remediation_steps", "apply_format_code", "__end__"]]: The next step in the workflow.
    """
    if category == "codebase":
        if pipeline_phase == "lint":
            return Command(goto="apply_format_code", update={"troubleshooting": troubleshooting}, graph=Command.PARENT)

        elif pipeline_phase == "unittest":
            return Command(goto="plan_approval", update={"troubleshooting": troubleshooting}, graph=Command.PARENT)

    return Command(goto=END, update={"need_manual_fix": True, "troubleshooting": troubleshooting}, graph=Command.PARENT)
