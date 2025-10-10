from typing import TYPE_CHECKING, Literal

from langchain_core.tools import tool
from langgraph.graph import END
from langgraph.types import Command

if TYPE_CHECKING:
    from .schemas import TroubleshootingDetail


@tool("complete_task", parse_docstring=True)
def complete_task(
    pipeline_phase: Literal["lint", "unittest", "other"], troubleshooting: list[TroubleshootingDetail]
) -> Command[Literal["plan_and_execute", "apply_format_code", "__end__"]]:
    """
    Call this tool to complete the task by providing the final categorization and troubleshooting details.

    Args:
        pipeline_phase: Identify the phase when the pipeline failed according to the job logs output.
            Use 'unittest' if the issue relates to unit tests, 'lint' for linting problems,
            or 'other' for any other phase.
        troubleshooting: A list of troubleshooting details for each identified error message. If there are no
            troubleshooting details, no error messages or the pipeline succeeded, return an empty list.

    Returns:
        Command[Literal["plan_and_execute", "apply_format_code", "__end__"]]: The next step in the workflow.
    """

    if any(item.category == "codebase" for item in troubleshooting):
        update_state = {"pipeline_phase": pipeline_phase, "troubleshooting": troubleshooting}

        if pipeline_phase == "lint":
            return Command(goto="apply_format_code", update=update_state, graph=Command.PARENT)

        elif pipeline_phase == "unittest":
            return Command(goto="plan_and_execute", update=update_state, graph=Command.PARENT)

    return Command(goto=END, update={"need_manual_fix": True, "troubleshooting": troubleshooting}, graph=Command.PARENT)
